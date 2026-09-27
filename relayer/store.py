"""中继器状态的 JSON 文件持久化，用于重启恢复。"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from .types import (
    CrossChainMessage,
    MessageRecord,
    MessageStatus,
    RejectReason,
    TransitionEntry,
)


class StateStore:
    """把消息记录与迁移日志持久化到本地 JSON 文件。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save(self, records: dict[str, MessageRecord],
             transitions: list[TransitionEntry],
             nonces: dict[str, int]) -> None:
        """原子地写入完整状态快照（临时文件 + rename）。"""
        payload = {
            "records": [
                {
                    "message": asdict(rec.message),
                    "status": rec.status.value,
                    "reject_reason": rec.reject_reason.value if rec.reject_reason else None,
                    "attempts": rec.attempts,
                    "history": list(rec.history),
                }
                for rec in records.values()
            ],
            "transitions": [asdict(t) for t in transitions],
            "nonces": dict(nonces),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def load(self) -> tuple[dict[str, MessageRecord], list[TransitionEntry], dict[str, int]] | None:
        """读取状态快照；文件不存在时返回 None。"""
        if not self.path.exists():
            return None
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        records: dict[str, MessageRecord] = {}
        for item in payload["records"]:
            msg = CrossChainMessage(**item["message"])
            reason = item["reject_reason"]
            records[msg.message_id] = MessageRecord(
                message=msg,
                status=MessageStatus(item["status"]),
                reject_reason=RejectReason(reason) if reason else None,
                attempts=item["attempts"],
                history=list(item["history"]),
            )
        transitions = [TransitionEntry(**t) for t in payload["transitions"]]
        nonces = {k: int(v) for k, v in payload["nonces"].items()}
        return records, transitions, nonces
