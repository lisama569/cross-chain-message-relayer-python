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
        """把消息写入区块，返回实际区块高度。

        message.source_block <= 0 表示由账本分配到下一个高度；
        否则写入指定高度（必须恰好是下一个高度且未被占用）。
        """
        with self._lock:
            height = message.source_block
            if height <= 0:
                height = self._height + 1
            if height in self._blocks:
                raise LedgerError(f"区块高度已存在: {height}")
            if height > self._height + 1:
                raise LedgerError(f"区块高度跳跃: {height} > {self._height + 1}")
            self._blocks[height] = [message.message_id]
            self._height = height
            return height

    def produce_block(self) -> int:
        """推进一个空区块，返回新高度。"""
        with self._lock:
            self._height += 1
            self._blocks[self._height] = []
            return self._height

    def is_final(self, block_height: int) -> bool:
        """判断区块是否已达到约定最终性（其后已有 confirmations 个确认块）。"""
        with self._lock:
            return (block_height in self._blocks
                    and self._height - block_height >= self.confirmations)

    def contains_message(self, message_id: str) -> bool:
        """消息是否仍存在于当前链视图中（回滚后为 False）。"""
        with self._lock:
            return any(message_id in ids for ids in self._blocks.values())

    def reorg(self, new_height: int) -> list[str]:
        """回滚到指定高度，返回被移除的消息 id 列表。"""
        with self._lock:
            if new_height < 0 or new_height >= self._height:
                raise LedgerError(f"非法回滚高度: {new_height}")
            removed: list[str] = []
            for h in sorted(h for h in self._blocks if h > new_height):
                removed.extend(self._blocks.pop(h))
            self._height = new_height
            return removed

    @property
    def height(self) -> int:
        with self._lock:
            return self._height

    # ---- 资产操作（均幂等：重复执行同一 message_id 不产生额外副作用）----
    def _apply(self, message: CrossChainMessage, op: str) -> None:
        with self._lock:
            if message.message_id in self._executed:
                return  # 幂等：同一消息不重复产生副作用
            amount = message.amount
            if amount <= 0:
                raise LedgerError(f"非法金额: {amount}")
            if op == "lock":
                if self.balances.get(message.sender, 0) < amount:
                    raise LedgerError(f"锁定失败：{message.sender} 余额不足")
                self.balances[message.sender] -= amount
                self.locked += amount
            elif op == "unlock":
                if self.locked < amount:
                    raise LedgerError("解锁失败：锁定托管余额不足")
                self.locked -= amount
                self.balances[message.recipient] = (
                    self.balances.get(message.recipient, 0) + amount)
            elif op == "mint":
                self.balances[message.recipient] = (
                    self.balances.get(message.recipient, 0) + amount)
                self.total_supply += amount
            elif op == "burn":
                if self.balances.get(message.sender, 0) < amount:
                    raise LedgerError(f"销毁失败：{message.sender} 余额不足")
                self.balances[message.sender] -= amount
                self.total_supply -= amount
            else:  # pragma: no cover
                raise LedgerError(f"未知操作: {op}")
            self._executed.add(message.message_id)
            assert self.check_conservation(), f"{self.name} 守恒被破坏: {op}"

    def lock(self, message: CrossChainMessage) -> None:
        self._apply(message, "lock")

    def unlock(self, message: CrossChainMessage) -> None:
        self._apply(message, "unlock")

    def mint(self, message: CrossChainMessage) -> None:
        self._apply(message, "mint")

    def burn(self, message: CrossChainMessage) -> None:
        self._apply(message, "burn")

    # ---- 守恒检查 ----
    def check_conservation(self) -> bool:
        """校验 sum(balances) + locked == total_supply 且余额非负。"""
        with self._lock:
            if any(v < 0 for v in self.balances.values()) or self.locked < 0:
                return False
            return sum(self.balances.values()) + self.locked == self.total_supply
