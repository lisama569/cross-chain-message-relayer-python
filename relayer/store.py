"""中继器状态的 JSON 文件持久化，用于重启恢复。"""
from __future__ import annotations

from pathlib import Path

from .types import MessageRecord, TransitionEntry


class StateStore:
    """把消息记录与迁移日志持久化到本地 JSON 文件。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save(self, records: dict[str, MessageRecord],
             transitions: list[TransitionEntry],
             nonces: dict[str, int]) -> None:
        """原子地写入完整状态快照。"""
        raise NotImplementedError

    def load(self) -> tuple[dict[str, MessageRecord], list[TransitionEntry], dict[str, int]] | None:
        """读取状态快照；文件不存在时返回 None。"""
        raise NotImplementedError
