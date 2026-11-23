from asyncio import Future, Lock
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiorwlock import RWLock

from src.models.registry import ModelInfo

def get_owner_id(channel_id: str, reply_target: str) -> str:
    """将通道实例和回复目标编码为唯一来源标识，后续扩展只调整此函数。"""
    return f"{channel_id}:{reply_target}"


@dataclass
class SessionTokenUsage:
    """保存用于窗口判断的主请求计数，以及会话已记录的实际消耗。"""

    last_total_tokens: int | None = None  # 上次普通主响应的 usage.total_tokens；安装新历史后清空。
    covered_items: int = 0  # 上次普通主响应的 last_total_tokens 已覆盖的消息数。
    total_reported_tokens: int = 0  # 该会话总历史消耗的token数，包括主请求与压缩，长期记忆检索和提取的不包括


@dataclass
class Session:
    """单个会话的内存结构"""

    path: Path  # 本会话 JSONL 路径，文件名作为会话标识。
    messages: list[Any] = field(default_factory=list)  # 当前有效模型上下文。
    ordinal: int = 0  # 下一条持久化记录的序号。
    turn_id: int = -1  # 当前用户轮次，首次实际输入时递增到 0。
    # 新会话首次写入时独占创建文件，已有会话继续追加。
    persisted: bool = True  # 文件是否已创建；新建时显式设置为 False。
    # 尚未收到结果的工具调用；用于中断恢复，绝不自动重放工具。
    pending_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    owner_id: str = ""  # 当前占用来源的唯一标识，由 get_owner_id 统一生成。
    expires_at: float = 0.0  # 单调时间的过期时刻；当前轮执行期间为无限大。
    pending_approval: Future[bool] | None = None  # 等待用户审核敏感工具请求
    tool_lock: RWLock = field(default_factory=RWLock)  # 同一会话内共享工具执行读写锁。
    approval_lock: Lock = field(default_factory=Lock)  # 同一时间只向用户展示一个待审批请求。
    token_usage: SessionTokenUsage = field(default_factory=SessionTokenUsage)  # 主请求窗口计数基准与累计实际消耗。
    summary_index: int | None = None  # 最近一次 compacted 末项摘要在有效历史中的位置；无压缩时为 None。
    model_info: ModelInfo | None = None  # 本会话当前使用的模型静态元信息，支持按会话切换模型。

    @property
    def id(self) -> str:
        """使用 JSONL 文件名中的 UUID 作为会话标识。"""
        return self.path.stem
