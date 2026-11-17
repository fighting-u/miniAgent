import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.tools.tools import PlanManager


@dataclass
class Session:
    """集中保存单个会话的历史、存储进度和运行状态。"""

    path: Path  # 本会话 JSONL 路径，文件名作为会话标识。
    messages: list[Any] = field(default_factory=list)  # 当前有效模型上下文。
    ordinal: int = 0  # 下一条持久化记录的序号。
    turn_id: int = -1  # 当前用户轮次，首次实际输入时递增到 0。
    # 新会话首次写入时独占创建文件，已有会话继续追加。
    persisted: bool = True  # 文件是否已创建；新建时显式设置为 False。
    # 尚未收到结果的工具调用；用于中断恢复，绝不自动重放工具。
    pending_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    # todo: 应该持久化锁，后续可能多进程
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # 覆盖整轮执行及历史修改。
    # todo: plan应该仅局限于每个turn
    plan: PlanManager = field(default_factory=PlanManager)  # 当前会话的运行时任务计划。

    @property
    def id(self) -> str:
        """使用 JSONL 文件名中的 UUID 作为会话标识。"""
        return self.path.stem
