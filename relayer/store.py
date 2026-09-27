"""中继器状态的 JSON 持久化，用于重启恢复。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .types import Message, MessageRecord, MessageStatus, RejectReason


class JsonStore:
    """以 JSON 文件持久化 MessageRecord，写盘采用 临时文件+原子替换。

    这样即使进程在写盘途中崩溃，也只会得到旧版本或新版本，不会得到截断文件。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save(self, records: dict[str, MessageRecord]) -> None:
        payload = {
            mid: self._record_to_dict(rec) for mid, rec in sorted(records.items())
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def load(self) -> dict[str, MessageRecord]:
        if not self.path.exists():
            return {}
        with open(self.path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return {mid: self._record_from_dict(d) for mid, d in payload.items()}

    # ---- 序列化 ----
    @staticmethod
    def _record_to_dict(rec: MessageRecord) -> dict:
        m = rec.message
        return {
            "message": {
                "src_chain": m.src_chain,
                "dst_chain": m.dst_chain,
                "sender": m.sender,
                "recipient": m.recipient,
                "asset": m.asset,
                "amount": m.amount,
                "nonce": m.nonce,
                "src_block": m.src_block,
                "message_id": m.message_id,
                "submitted_at": m.submitted_at,
            },
            "status": rec.status.value,
            "reject_reason": rec.reject_reason.value if rec.reject_reason else None,
            "finality_proof": rec.finality_proof,
            "history": list(rec.history),
        }

    @staticmethod
    def _record_from_dict(d: dict) -> MessageRecord:
        m = d["message"]
        msg = Message(
            src_chain=m["src_chain"],
            dst_chain=m["dst_chain"],
            sender=m["sender"],
            recipient=m["recipient"],
            asset=m["asset"],
            amount=m["amount"],
            nonce=m["nonce"],
            src_block=m["src_block"],
            message_id=m["message_id"],
            submitted_at=m["submitted_at"],
        )
        reason = d.get("reject_reason")
        return MessageRecord(
            message=msg,
            status=MessageStatus(d["status"]),
            reject_reason=RejectReason(reason) if reason else None,
            finality_proof=d.get("finality_proof"),
            history=list(d.get("history", [])),
        )
