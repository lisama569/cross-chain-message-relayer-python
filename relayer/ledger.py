"""本地链账本模拟：区块、最终性、回滚与资产操作。"""
from __future__ import annotations

import threading

from .types import AssetOp, ChainEvent


class LedgerError(Exception):
    """账本操作失败（如余额不足、重复执行、回滚穿越最终性）。"""


class LocalChainLedger:
    """单条本地链的账本。

    - 维护账户余额（含锁定子账户）与已执行 (message_id, op) 集合（幂等）。
    - 以区块高度推进，finalized_depth 个确认之前的区块视为最终。
    - 支持回滚到指定高度（模拟链重组），已最终的事件不可撤销。
    """

    def __init__(self, chain_id: str, finalized_depth: int = 2) -> None:
        self.chain_id = chain_id
        self.finalized_depth = finalized_depth
        self._lock = threading.RLock()
        self._height = 0
        self._balances: dict[tuple[str, str], int] = {}
        self._locked: dict[tuple[str, str], int] = {}
        self._executed: set[tuple[str, str]] = set()  # (message_id, op)
        self._events: list[ChainEvent] = []

    # ---- 链推进与最终性 ----
    def mine_block(self) -> int:
        with self._lock:
            self._height += 1
            return self._height

    @property
    def height(self) -> int:
        with self._lock:
            return self._height

    @property
    def finalized_height(self) -> int:
        with self._lock:
            return max(0, self._height - self.finalized_depth)

    def is_final(self, block_height: int) -> bool:
        with self._lock:
            return 0 < block_height <= self.finalized_height

    def rollback(self, to_height: int) -> list[ChainEvent]:
        """回滚到指定高度，撤销未最终的事件，返回被撤销的事件列表。

        回滚目标不得低于 finalized_height —— 已最终的结果不可回退。
        """
        with self._lock:
            if to_height < self.finalized_height:
                raise LedgerError(
                    f"cannot rollback {self.chain_id} to {to_height}: "
                    f"finalized_height={self.finalized_height}"
                )
            reverted: list[ChainEvent] = []
            kept: list[ChainEvent] = []
            for ev in self._events:
                if ev.block_height > to_height:
                    reverted.append(ev)
                else:
                    kept.append(ev)
            for ev in reversed(reverted):
                self._undo(ev)
                self._executed.discard((ev.message_id, ev.op.value))
            self._events = kept
            self._height = to_height
            return reverted

    def _undo(self, ev: ChainEvent) -> None:
        key = (ev.account, ev.asset)
        if ev.op is AssetOp.MINT:
            self._balances[key] = self._balances.get(key, 0) - ev.amount
        elif ev.op is AssetOp.BURN:
            self._balances[key] = self._balances.get(key, 0) + ev.amount
        elif ev.op is AssetOp.LOCK:
            self._locked[key] = self._locked.get(key, 0) - ev.amount
            self._balances[key] = self._balances.get(key, 0) + ev.amount
        elif ev.op is AssetOp.UNLOCK:
            self._locked[key] = self._locked.get(key, 0) + ev.amount
            self._balances[key] = self._balances.get(key, 0) - ev.amount

    # ---- 资产操作（均在当前高度记录事件）----
    def _apply(self, op: AssetOp, account: str, asset: str, amount: int,
               message_id: str) -> ChainEvent:
        with self._lock:
            if amount <= 0:
                raise LedgerError(f"amount must be positive, got {amount}")
            exec_key = (message_id, op.value)
            if exec_key in self._executed:
                raise LedgerError(
                    f"{op.value} already executed for message {message_id}"
                )
            key = (account, asset)
            if op is AssetOp.MINT:
                self._balances[key] = self._balances.get(key, 0) + amount
            elif op is AssetOp.BURN:
                if self._balances.get(key, 0) < amount:
                    raise LedgerError(f"insufficient balance to burn {amount} {asset}")
                self._balances[key] -= amount
            elif op is AssetOp.LOCK:
                if self._balances.get(key, 0) < amount:
                    raise LedgerError(f"insufficient balance to lock {amount} {asset}")
                self._balances[key] -= amount
                self._locked[key] = self._locked.get(key, 0) + amount
            elif op is AssetOp.UNLOCK:
                if self._locked.get(key, 0) < amount:
                    raise LedgerError(f"insufficient locked to unlock {amount} {asset}")
                self._locked[key] -= amount
                self._balances[key] = self._balances.get(key, 0) + amount
            ev = ChainEvent(
                chain=self.chain_id, op=op, account=account, asset=asset,
                amount=amount, message_id=message_id, block_height=self._height,
            )
            self._executed.add(exec_key)
            self._events.append(ev)
            return ev

    def mint(self, account: str, asset: str, amount: int, message_id: str) -> ChainEvent:
        return self._apply(AssetOp.MINT, account, asset, amount, message_id)

    def burn(self, account: str, asset: str, amount: int, message_id: str) -> ChainEvent:
        return self._apply(AssetOp.BURN, account, asset, amount, message_id)

    def lock(self, account: str, asset: str, amount: int, message_id: str) -> ChainEvent:
        return self._apply(AssetOp.LOCK, account, asset, amount, message_id)

    def unlock(self, account: str, asset: str, amount: int, message_id: str) -> ChainEvent:
        return self._apply(AssetOp.UNLOCK, account, asset, amount, message_id)

    # ---- 查询 ----
    def balance_of(self, account: str, asset: str) -> int:
        with self._lock:
            return self._balances.get((account, asset), 0)

    def locked_of(self, account: str, asset: str) -> int:
        with self._lock:
            return self._locked.get((account, asset), 0)

    def total_supply(self, asset: str) -> int:
        """流通总量 + 锁定量（锁定仍是已铸造资产，只是不可转移）。"""
        with self._lock:
            total = 0
            for (account, a), v in self._balances.items():
                if a == asset:
                    total += v
            for (account, a), v in self._locked.items():
                if a == asset:
                    total += v
            return total

    def circulating_supply(self, asset: str) -> int:
        """流通量（不含锁定）。跨链守恒断言用：各链流通量之和恒定。"""
        with self._lock:
            return sum(v for (a, ast), v in self._balances.items() if ast == asset)

    def has_executed(self, message_id: str, op: AssetOp | None = None) -> bool:
        with self._lock:
            if op is not None:
                return (message_id, op.value) in self._executed
            return any(mid == message_id for mid, _ in self._executed)

    def snapshot(self) -> dict:
        """返回一致的状态快照（持锁拷贝）。"""
        with self._lock:
            return {
                "chain_id": self.chain_id,
                "height": self._height,
                "finalized_height": self.finalized_height,
                "balances": {f"{a}/{b}": v for (a, b), v in sorted(self._balances.items())},
                "locked": {f"{a}/{b}": v for (a, b), v in sorted(self._locked.items())},
                "executed": sorted(f"{m}:{o}" for m, o in self._executed),
                "events": [
                    {
                        "block": ev.block_height, "op": ev.op.value,
                        "account": ev.account, "asset": ev.asset,
                        "amount": ev.amount, "message_id": ev.message_id,
                    }
                    for ev in self._events
                ],
            }
