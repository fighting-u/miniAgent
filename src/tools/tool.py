from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from src.tools.local_tools import Plan


@dataclass
class ToolContext:
    """保存工具调用所需的本轮状态，由 Agent 为每个 turn 单独创建。"""

    plan: "Plan"  # 当前 turn 的任务计划。
    current_working_directory: Path  # 当前 turn 用于解析相对路径和补全工具工作目录的基准目录。


@dataclass
class Tool:
    """统一描述工具定义及异步执行入口，不区分本地与 MCP 来源。"""

    name: str  # 提供给模型并用于注册的工具名称。
    description: str  # 提供给模型的工具说明。
    parameters: dict[str, Any]  # 工具参数的 JSON Schema。
    execute: Callable[[dict[str, Any], ToolContext], Awaitable[Any]]  # 接收模型参数和本轮状态的异步执行入口。
    strict: bool | None = None  # None 表示不向模型请求显式传入 strict。
