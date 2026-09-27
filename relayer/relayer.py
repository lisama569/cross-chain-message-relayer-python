"""中继器核心：提交、最终性确认、投递执行与恢复。"""
from __future__ import annotations

import threading
from dataclasses import replace

from .ledger import LedgerError, LocalChainLedger
from .store import JsonStore
from .types import Message, MessageRecord, MessageStatus, RejectReason


class SubmitRejected(Exception):
    """提交/投递被拒绝，携带可区分的拒绝原因。"""

    def __init__(self, reason: RejectReason, detail: str = "") -> None:
        super().__init__(f"{reason.value}: {detail}")
        self.reason = reason


class Relayer:
    """跨链消息中继器。

    语义：
    - 同一 message_id 重复提交且负载一致 -> 幂等返回原记录（历史记录标注
      DUPLICATE_MESSAGE）；负载不一致 -> SubmitRejected(REPLAY_CONFLICT)。
    - nonce 在 (src, dst, sender) 维度必须严格递增，乱序 ->
      SubmitRejected(NONCE_OUT_OF_ORDER)。
    - 仅当 src_block 在来源链上达到最终性后才允许执行，投递前会重新校验，
      期间发生回滚则终态拒绝并抛出 SubmitRejected(SOURCE_REORGED)。
    - 执行方向由 asset_home 决定：目标是资产原生链 -> 解锁(UNLOCK)，
      否则 -> 铸造(MINT)。来源侧托管由 prepare_source 完成：
      原生链锁定(LOCK)，非原生链销毁(BURN)，形成完整闭环。
    """

    def __init__(
        self,
        chains: dict[str, LocalChainLedger],
        store: JsonStore,
        asset_home: dict[str, str] | None = None,
    ) -> None:
        self.chains = chains
        self.store = store
        self.asset_home = asset_home or {}
        self._lock = threading.RLock()
        self._records: dict[str, MessageRecord] = {}
        self._nonces: dict[tuple[str, str, str], int] = {}

    # ---- 内部工具 ----
    def _lane(self, m: Message) -> tuple[str, str, str]:
        return (m.src_chain, m.dst_chain, m.sender)

    def _transition(self, rec: MessageRecord, to: MessageStatus, note: str = "") -> None:
        entry = f"{rec.status.value}->{to.value}"
        if note:
            entry += f"({note})"
        rec.history.append(entry)
        rec.status = to

    def _save(self) -> None:
        self.store.save(self._records)

    # ---- 来源侧托管：锁定或销毁，返回带 src_block 的消息 ----
    def prepare_source(self, msg: Message) -> Message:
        """在来源链执行托管操作（原生链 LOCK / 非原生链 BURN），

        返回 src_block 指向托管事件所在区块的新消息。
        """
        with self._lock:
            src = self.chains[msg.src_chain]
            if self.asset_home.get(msg.asset, msg.src_chain) == msg.src_chain:
                ev = src.lock(msg.sender, msg.asset, msg.amount, msg.message_id)
            else:
                ev = src.burn(msg.sender, msg.asset, msg.amount, msg.message_id)
            return replace(msg, src_block=ev.block_height)

    # ---- 提交 ----
    def submit(self, msg: Message) -> MessageRecord:
        with self._lock:
            existing = self._records.get(msg.message_id)
            if existing is not None:
                if existing.message == msg:
                    # 幂等：负载一致的重复提交不产生额外副作用
                    existing.history.append(
                        f"duplicate submit ignored({RejectReason.DUPLICATE_MESSAGE.value})"
                    )
                    self._save()
                    return existing
                raise SubmitRejected(
                    RejectReason.REPLAY_CONFLICT,
                    f"message_id {msg.message_id} reused with different payload",
                )
            if msg.amount <= 0:
                rec = MessageRecord(message=msg)
                rec.reject_reason = RejectReason.INVALID_PAYLOAD
                self._transition(rec, MessageStatus.REJECTED, "amount must be positive")
                self._records[msg.message_id] = rec
                self._save()
                return rec
            lane = self._lane(msg)
            expected = self._nonces.get(lane, 0) + 1
            if msg.nonce != expected:
                raise SubmitRejected(
                    RejectReason.NONCE_OUT_OF_ORDER,
                    f"lane {lane} expects nonce {expected}, got {msg.nonce}",
                )
            rec = MessageRecord(message=msg)
            rec.history.append(
                f"accepted: lane={lane} nonce={msg.nonce} src_block={msg.src_block}"
            )
            self._records[msg.message_id] = rec
            self._nonces[lane] = msg.nonce
            self._save()
            return rec

    # ---- 最终性确认 ----
    def confirm(self, message_id: str) -> MessageRecord:
        with self._lock:
            rec = self._records[message_id]
            if rec.status in (MessageStatus.FINALIZED, MessageStatus.EXECUTED,
                              MessageStatus.REJECTED):
                return rec
            src = self.chains[rec.message.src_chain]
            fh = src.finalized_height
            if src.is_final(rec.message.src_block):
                rec.finality_proof = (
                    f"src_block={rec.message.src_block}<=finalized_height={fh} "
                    f"on {src.chain_id}"
                )
                self._transition(rec, MessageStatus.FINALIZED, rec.finality_proof)
                self._save()
            else:
                rec.history.append(
                    f"awaiting finality: src_block={rec.message.src_block} "
                    f"finalized_height={fh} on {src.chain_id}"
                )
            return rec

    # ---- 投递执行 ----
    def deliver(self, message_id: str) -> MessageRecord:
        with self._lock:
            rec = self._records[message_id]
            if rec.status is MessageStatus.EXECUTED:
                return rec  # 幂等：已执行不重放
            if rec.status is MessageStatus.REJECTED:
                raise SubmitRejected(rec.reject_reason or RejectReason.INVALID_PAYLOAD,
                                     "record is rejected")
            if rec.status is MessageStatus.PENDING:
                rec = self.confirm(message_id)
            if rec.status is MessageStatus.PENDING:
                raise SubmitRejected(RejectReason.SOURCE_NOT_FINAL,
                                     f"message {message_id} not final on source")
            # 投递前重新校验最终性：确认后可能发生回滚
            src = self.chains[rec.message.src_chain]
            if not src.is_final(rec.message.src_block):
                # 来源托管事件已随回滚消失，消息失去资产背书，终态拒绝
                rec.finality_proof = None
                rec.reject_reason = RejectReason.SOURCE_REORGED
                self._transition(rec, MessageStatus.REJECTED,
                                 RejectReason.SOURCE_REORGED.value)
                self._save()
                raise SubmitRejected(RejectReason.SOURCE_REORGED,
                                     f"message {message_id} lost finality")
            dst = self.chains[rec.message.dst_chain]
            home = self.asset_home.get(rec.message.asset, rec.message.src_chain)
            try:
                if home == rec.message.dst_chain:
                    dst.unlock(rec.message.recipient, rec.message.asset,
                               rec.message.amount, message_id)
                else:
                    dst.mint(rec.message.recipient, rec.message.asset,
                             rec.message.amount, message_id)
            except LedgerError as e:
                if "already executed" in str(e):
                    # 崩溃恢复：链上已执行但记录未落盘，直接对齐为 EXECUTED
                    rec.history.append(f"chain already applied: {e}")
                    self._transition(rec, MessageStatus.EXECUTED, "reconciled")
                else:
                    self._transition(rec, MessageStatus.FAILED_RETRYABLE, str(e))
                self._save()
                return rec
            self._transition(rec, MessageStatus.EXECUTED,
                             f"dst_block={dst.height} on {dst.chain_id}")
            self._save()
            return rec

    def process(self, msg: Message) -> MessageRecord:
        """便捷入口：submit -> confirm -> deliver（未最终则停在 PENDING）。"""
        rec = self.submit(msg)
        if rec.status is MessageStatus.REJECTED:
            return rec
        rec = self.confirm(msg.message_id)
        if rec.status in (MessageStatus.FINALIZED, MessageStatus.FAILED_RETRYABLE):
            rec = self.deliver(msg.message_id)
        return rec

    # ---- 恢复 ----
    def recover(self) -> list[MessageRecord]:
        """重启恢复：加载持久化记录，重建 nonce，继续推进挂起任务。"""
        with self._lock:
            self._records = self.store.load()
            self._nonces = {}
            for rec in self._records.values():
                if rec.status is not MessageStatus.REJECTED:
                    lane = self._lane(rec.message)
                    self._nonces[lane] = max(self._nonces.get(lane, 0),
                                             rec.message.nonce)
            advanced: list[MessageRecord] = []
            for rec in list(self._records.values()):
                if rec.status is MessageStatus.PENDING:
                    before = rec.status
                    rec = self.confirm(rec.message.message_id)
                    if rec.status is MessageStatus.FINALIZED:
                        rec = self.deliver(rec.message.message_id)
                    if rec.status is not before:
                        advanced.append(rec)
                elif rec.status in (MessageStatus.FINALIZED,
                                    MessageStatus.FAILED_RETRYABLE):
                    rec = self.deliver(rec.message.message_id)
                    advanced.append(rec)
            return advanced

    def on_source_rollback(self, chain_id: str, to_height: int) -> list[MessageRecord]:
        """来源链回滚：src_block 被回滚的未执行消息终态拒绝(SOURCE_REORGED)。

        托管事件已随回滚撤销、资金返还来源账户，消息失去资产背书，
        不可继续执行；EXECUTED 记录不受影响（其来源区块已最终，不可回滚）。
        """
        with self._lock:
            self.chains[chain_id].rollback(to_height)
            affected: list[MessageRecord] = []
            for rec in self._records.values():
                if (rec.message.src_chain == chain_id
                        and rec.message.src_block > to_height
                        and rec.status in (MessageStatus.PENDING,
                                           MessageStatus.FINALIZED,
                                           MessageStatus.FAILED_RETRYABLE)):
                    rec.finality_proof = None
                    rec.reject_reason = RejectReason.SOURCE_REORGED
                    rec.history.append(
                        f"source reorged: {chain_id} rolled back to {to_height}"
                    )
                    self._transition(rec, MessageStatus.REJECTED,
                                     RejectReason.SOURCE_REORGED.value)
                    affected.append(rec)
            if affected:
                self._save()
            return affected

    # ---- 查询 ----
    def get(self, message_id: str) -> MessageRecord | None:
        with self._lock:
            return self._records.get(message_id)

    def records(self) -> dict[str, MessageRecord]:
        with self._lock:
            return dict(self._records)
