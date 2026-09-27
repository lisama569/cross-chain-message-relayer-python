"""跨链消息中继器：提交登记、最终性确认、按序执行、恢复续作。"""
from __future__ import annotations

import dataclasses
import threading

from .ledger import LedgerError, LocalChainLedger
from .store import StateStore
from .types import (
    CrossChainMessage,
    MessageRecord,
    MessageStatus,
    RejectReason,
    TransitionEntry,
)

TERMINAL = {MessageStatus.EXECUTED, MessageStatus.REJECTED}


class Relayer:
    """在多条本地链账本之间中继消息。

    流程：submit 登记（来源侧 lock/burn）-> 来源链达到最终性 ->
    按 nonce 顺序在目标链执行（mint/unlock）。
    所有状态迁移写入日志并持久化，支持重启恢复与续作。

    资产闭环约定：home_chain 是资产原生链，消息离开 home 时锁定、
    回到 home 时解锁；在非原生链侧对应销毁与铸造。
    """

    def __init__(self, chains: dict[str, LocalChainLedger],
                 home_chain: str,
                 store: StateStore | None = None) -> None:
        if home_chain not in chains:
            raise ValueError(f"home_chain 不存在: {home_chain}")
        self.chains = chains
        self.home_chain = home_chain
        self.store = store
        self._lock = threading.RLock()
        self.records: dict[str, MessageRecord] = {}
        self.transitions: list[TransitionEntry] = []
        # 每条来源链已执行到的 nonce（顺序语义）
        self.nonces: dict[str, int] = {}

    # ---- 内部工具 ----
    def _transition(self, record: MessageRecord, to: MessageStatus,
                    reason: str,
                    reject: RejectReason | None = None) -> None:
        entry = TransitionEntry(record.message.message_id,
                                record.status.value, to.value, reason)
        self.transitions.append(entry)
        record.history.append(f"{entry.from_status}->{entry.to_status}: {reason}")
        record.status = to
        record.reject_reason = reject

    def _persist(self) -> None:
        if self.store is not None:
            self.store.save(self.records, self.transitions, self.nonces)

    def _expected_nonce(self, source_chain: str) -> int:
        seen = [self.nonces.get(source_chain, 0)]
        # 被拒绝的消息不占位，不参与 nonce 推进
        seen += [r.message.nonce for r in self.records.values()
                 if r.message.source_chain == source_chain
                 and r.status is not MessageStatus.REJECTED]
        return max(seen) + 1

    # ---- 提交 ----
    def submit(self, message: CrossChainMessage) -> MessageRecord:
        """登记消息；识别重复提交、重放与乱序并给出可区分的拒绝原因。

        成功登记时在来源链提交区块并执行来源侧资产操作（lock/burn）。
        """
        with self._lock:
            existing = self.records.get(message.message_id)
            if existing is not None:
                # 已执行消息的重放 / 未决消息的重复提交：均不改变现有状态，
                # 只追加可区分的拒绝记录。
                reason = (RejectReason.REPLAY_AFTER_EXECUTION
                          if existing.status is MessageStatus.EXECUTED
                          else RejectReason.DUPLICATE_MESSAGE)
                existing.history.append(f"重放/重复提交被拒绝: {reason.value}")
                self.transitions.append(TransitionEntry(
                    message.message_id, existing.status.value,
                    existing.status.value, reason.value))
                self._persist()
                return existing

            expected = self._expected_nonce(message.source_chain)
            if message.nonce != expected:
                record = MessageRecord(message, MessageStatus.REJECTED,
                                       RejectReason.OUT_OF_ORDER)
                record.history.append(
                    f"NONE->REJECTED: 乱序 nonce={message.nonce}, 期望 {expected}")
                self.transitions.append(TransitionEntry(
                    message.message_id, "NONE", MessageStatus.REJECTED.value,
                    f"{RejectReason.OUT_OF_ORDER.value}: nonce={message.nonce}, "
                    f"期望 {expected}"))
                self.records[message.message_id] = record
                self._persist()
                return record

            source = self.chains[message.source_chain]
            # 来源侧资产操作（幂等）：离开原生链锁定，否则销毁
            if message.source_chain == self.home_chain:
                source.lock(message)
            else:
                source.burn(message)
            actual_height = source.commit_message(message)
            if actual_height != message.source_block:
                message = dataclasses.replace(message, source_block=actual_height)

            record = MessageRecord(message)
            record.history.append(
                f"NONE->PENDING_FINALITY: 登记于 {message.source_chain} "
                f"区块 {message.source_block}")
            self.transitions.append(TransitionEntry(
                message.message_id, "NONE",
                MessageStatus.PENDING_FINALITY.value,
                f"提交于区块 {message.source_block}"))
            self.records[message.message_id] = record
            self._persist()
            return record

    # ---- 最终性确认 ----
    def poll_finality(self) -> list[str]:
        """检查挂起消息的来源区块最终性，返回进入 READY 的消息 id。"""
        with self._lock:
            ready: list[str] = []
            for record in self.records.values():
                if record.status is not MessageStatus.PENDING_FINALITY:
                    continue
                msg = record.message
                source = self.chains[msg.source_chain]
                if not source.contains_message(msg.message_id):
                    self._transition(record, MessageStatus.REJECTED,
                                     "来源区块被回滚，消息失效",
                                     RejectReason.SOURCE_REORGED)
                    continue
                if source.is_final(msg.source_block):
                    basis = (f"最终性依据: 区块 {msg.source_block} 已获得 "
                             f"{source.confirmations} 个确认 "
                             f"(当前高度 {source.height})")
                    self._transition(record, MessageStatus.READY, basis)
                    ready.append(msg.message_id)
            self._persist()
            return ready

    # ---- 执行 ----
    def execute_ready(self) -> list[str]:
        """按 (来源链, nonce) 确定顺序执行所有 READY 消息，返回成功执行的消息 id。"""
        with self._lock:
            pending = sorted(
                (r for r in self.records.values()
                 if r.status is MessageStatus.READY),
                key=lambda r: (r.message.source_chain, r.message.nonce))
            executed: list[str] = []
            for record in pending:
                msg = record.message
                if msg.nonce != self.nonces.get(msg.source_chain, 0) + 1:
                    record.history.append(
                        f"跳过执行: nonce={msg.nonce} 不是该来源链下一个期望序号")
                    continue
                record.attempts += 1
                target = self.chains[msg.target_chain]
                try:
                    if msg.target_chain == self.home_chain:
                        target.unlock(msg)
                    else:
                        target.mint(msg)
                except LedgerError as exc:
                    self._transition(record, MessageStatus.EXECUTION_FAILED,
                                     f"第 {record.attempts} 次执行失败: {exc}")
                    continue
                self.nonces[msg.source_chain] = msg.nonce
                self._transition(record, MessageStatus.EXECUTED,
                                 f"在 {msg.target_chain} 执行成功 "
                                 f"(第 {record.attempts} 次尝试)")
                executed.append(msg.message_id)
                self._persist()  # 每条落盘，崩溃可恢复
            self._persist()
            return executed

    # ---- 恢复 ----
    def recover(self) -> list[str]:
        """重启后恢复：加载快照，处理链回滚，续作挂起任务。

        已 EXECUTED 的结果绝不回退；返回恢复后继续推进的消息 id。
        """
        with self._lock:
            if self.store is not None:
                loaded = self.store.load()
                if loaded is not None:
                    self.records, self.transitions, self.nonces = loaded
            for record in self.records.values():
                if record.status in TERMINAL:
                    continue
                source = self.chains[record.message.source_chain]
                if not source.contains_message(record.message.message_id):
                    self._transition(record, MessageStatus.REJECTED,
                                     "恢复时发现来源区块已回滚",
                                     RejectReason.SOURCE_REORGED)
            self._persist()
            resumed = self.poll_finality()
            resumed += self.execute_ready()
            return resumed

    # ---- 观测 ----
    def snapshot(self) -> dict:
        """返回一致的状态快照（各链余额/锁定/总量 + 消息状态）。"""
        with self._lock:
            return {
                "chains": {
                    name: {
                        "height": c.height,
                        "locked": c.locked,
                        "total_supply": c.total_supply,
                        "balances": dict(c.balances),
                        "conservation": c.check_conservation(),
                    }
                    for name, c in self.chains.items()
                },
                "messages": {mid: r.status.value
                             for mid, r in self.records.items()},
                "nonces": dict(self.nonces),
            }
