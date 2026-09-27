"""跨链消息中继的核心类型定义。

只包含数据结构与枚举，不包含任何业务逻辑。
"""
from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


class MessageStatus(enum.Enum):
    """消息在中继器中的生命周期状态。"""

    PENDING = "PENDING"            # 已登记，等待来源链最终性
    FINALIZED = "FINALIZED"        # 来源侧已达最终性，等待投递执行
    EXECUTED = "EXECUTED"          # 目标侧执行完成（终态，不可回退）
    REJECTED = "REJECTED"          # 被拒绝（终态，附拒绝原因）
    FAILED_RETRYABLE = "FAILED_RETRYABLE"  # 执行失败但可重试


class RejectReason(enum.Enum):
    """可区分的拒绝原因。"""

    DUPLICATE_MESSAGE = "DUPLICATE_MESSAGE"        # 重复提交（同 message_id 已存在且负载一致）
    REPLAY_CONFLICT = "REPLAY_CONFLICT"            # 重放冲突（同 message_id 但负载不一致）
    NONCE_OUT_OF_ORDER = "NONCE_OUT_OF_ORDER"      # nonce 乱序，违反顺序语义
    SOURCE_NOT_FINAL = "SOURCE_NOT_FINAL"          # 来源侧尚未达到最终性
    SOURCE_REORGED = "SOURCE_REORGED"              # 来源区块被回滚，最终性依据失效
    INVALID_PAYLOAD = "INVALID_PAYLOAD"            # 负载非法（金额非正等）


class AssetOp(enum.Enum):
    """资产操作类型，构成 锁定->铸造 / 销毁->解锁 闭环。"""

    LOCK = "LOCK"        # 来源链锁定
    MINT = "MINT"        # 目标链铸造
    BURN = "BURN"        # 目标链销毁
    UNLOCK = "UNLOCK"    # 来源链解锁


@dataclass(frozen=True)
class Message:
    """跨链消息。message_id 全局唯一，nonce 在 (src, dst, sender) 维度单调递增。"""

    src_chain: str
    dst_chain: str
    sender: str
    recipient: str
    asset: str
    amount: int
    nonce: int
    src_block: int                      # 来源链包含该消息的区块高度
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    submitted_at: float = field(default_factory=time.time)


@dataclass
class MessageRecord:
    """中继器为每条消息维护的处理记录（可持久化、可追踪）。"""

    message: Message
    status: MessageStatus = MessageStatus.PENDING
    reject_reason: Optional[RejectReason] = None
    finality_proof: Optional[str] = None   # 最终性依据（如 "src_block<=finalized_height"）
    history: list[str] = field(default_factory=list)  # 状态迁移日志


@dataclass(frozen=True)
class ChainEvent:
    """链上资产事件，写入账本事件日志供审计。"""

    chain: str
    op: AssetOp
    account: str
    asset: str
    amount: int
    message_id: str
    block_height: int
