"""跨链消息中继器：提交登记、最终性确认、按序执行、恢复续作。"""
from __future__ import annotations

import threading

from .ledger import LocalChainLedger
from .store import StateStore
from .types import (
    CrossChainMessage,
    MessageRecord,
    MessageStatus,
    RejectReason,
    TransitionEntry,
)


class Relayer:
    """在两条本地链账本之间中继消息。

    流程：submit 登记 -> 来源链达到最终性 -> 按 nonce 顺序在目标链执行。
    所有状态迁移写入日志并持久化，支持重启恢复与续作。
    """

    def __init__(self, chains: dict[str, LocalChainLedger],
                 store: StateStore | None = None) -> None:
        self.chains = chains
        self.store = store
        self._lock = threading.RLock()
        self.records: dict[str, MessageRecord] = {}
        self.transitions: list[TransitionEntry] = []
        # 每条来源链已执行到的 nonce（顺序语义）
        self.nonces: dict[str, int] = {}

    # ---- 提交 ----
    def submit(self, message: CrossChainMessage) -> MessageStatus:
        """登记消息；识别重复提交、重放与乱序并给出可区分的拒绝原因。"""
        raise NotImplementedError

    # ---- 最终性确认 ----
    def poll_finality(self) -> list[str]:
        """检查挂起消息的来源区块最终性，返回进入 READY 的消息 id。"""
        raise NotImplementedError

    # ---- 执行 ----
    def execute_ready(self) -> list[str]:
        """按 nonce 顺序执行所有 READY 消息，返回成功执行的消息 id。"""
        raise NotImplementedError

    # ---- 恢复 ----
    def recover(self) -> list[str]:
        """重启后恢复：加载快照，处理链回滚，续作挂起任务。"""
        raise NotImplementedError

    # ---- 观测 ----
    def snapshot(self) -> dict:
        """返回一致的状态快照（各链余额/锁定/总量 + 消息状态）。"""
        raise NotImplementedError
