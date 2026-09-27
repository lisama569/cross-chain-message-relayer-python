"""共享类型定义：消息、状态机、拒绝原因与状态迁移日志。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class MessageStatus(enum.Enum):
    """消息生命周期状态机。"""

    PENDING_FINALITY = "PENDING_FINALITY"   # 已登记，等待来源链最终性
    READY = "READY"                         # 已达最终性，等待执行
    EXECUTED = "EXECUTED"                   # 已在目标链执行（终态）
    EXECUTION_FAILED = "EXECUTION_FAILED"   # 执行失败，可重试
    REJECTED = "REJECTED"                   # 被拒绝（终态）


class RejectReason(enum.Enum):
    """可区分的拒绝原因。"""

    DUPLICATE_MESSAGE = "DUPLICATE_MESSAGE"         # 重复提交（消息标识已存在）
    REPLAY_AFTER_EXECUTION = "REPLAY_AFTER_EXECUTION"  # 已执行后的重放
    OUT_OF_ORDER = "OUT_OF_ORDER"                   # 乱序：nonce 与期望不符
    SOURCE_REORGED = "SOURCE_REORGED"               # 来源区块被回滚，消息失效
    NOT_FINAL = "NOT_FINAL"                         # 尚未达到最终性


@dataclass(frozen=True)
class CrossChainMessage:
    """跨链消息（锁定-铸造 或 销毁-解锁）。"""

    message_id: str          # 全局唯一消息标识
    source_chain: str
    target_chain: str
    nonce: int               # 来源链上的单调序号，用于顺序语义
    sender: str
    recipient: str
    amount: int
    source_block: int        # 消息所在的来源链区块高度


@dataclass
class MessageRecord:
    """中继器为每条消息维护的处理记录。"""

    message: CrossChainMessage
    status: MessageStatus = MessageStatus.PENDING_FINALITY
    reject_reason: RejectReason | None = None
    attempts: int = 0
    history: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TransitionEntry:
    """一条可追踪的状态迁移日志。"""

    message_id: str
    from_status: str
    to_status: str
    reason: str
