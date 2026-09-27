"""本地链账本：模拟区块、最终性、回滚与锁定/铸造/销毁/解锁的资产守恒。"""
from __future__ import annotations

import threading

from .types import CrossChainMessage


class LedgerError(Exception):
    """账本操作失败（如余额不足、重复执行）。"""


class LocalChainLedger:
    """单条本地链的账本。

    - 维护一条可回滚的区块链，区块按 confirmations 确认数达到最终性；
    - 维护资产视图：locked（锁定托管）、balances（账户余额）、total_supply；
    - 守恒不变量：sum(balances) + locked == total_supply（铸造/销毁成对调整）。
    """

    def __init__(self, name: str, confirmations: int = 2,
                 genesis_balances: dict[str, int] | None = None) -> None:
        self.name = name
        self.confirmations = confirmations
        self._lock = threading.RLock()
        # 区块内容：height -> 该高度提交的消息 id 列表
        self._blocks: dict[int, list[str]] = {}
        self._height = 0
        self.locked = 0
        self.balances: dict[str, int] = dict(genesis_balances or {})
        self.total_supply = sum(self.balances.values())
        self._executed: set[str] = set()  # 已执行的 message_id（幂等）

    # ---- 区块与最终性 ----
    def commit_message(self, message: CrossChainMessage) -> int:
        """把消息写入指定高度的区块，返回区块高度。"""
        raise NotImplementedError

    def produce_block(self) -> int:
        """推进一个空区块，返回新高度。"""
        raise NotImplementedError

    def is_final(self, block_height: int) -> bool:
        """判断区块是否已达到约定最终性。"""
        raise NotImplementedError

    def contains_message(self, message_id: str) -> bool:
        """消息是否仍存在于当前链视图中（回滚后为 False）。"""
        raise NotImplementedError

    def reorg(self, new_height: int) -> list[str]:
        """回滚到指定高度，返回被移除的消息 id 列表。"""
        raise NotImplementedError

    @property
    def height(self) -> int:
        raise NotImplementedError

    # ---- 资产操作（均幂等：重复执行同一 message_id 不产生额外副作用）----
    def lock(self, message: CrossChainMessage) -> None:
        raise NotImplementedError

    def unlock(self, message: CrossChainMessage) -> None:
        raise NotImplementedError

    def mint(self, message: CrossChainMessage) -> None:
        raise NotImplementedError

    def burn(self, message: CrossChainMessage) -> None:
        raise NotImplementedError

    # ---- 守恒检查 ----
    def check_conservation(self) -> bool:
        """校验 sum(balances) + locked == total_supply 且余额非负。"""
        raise NotImplementedError
