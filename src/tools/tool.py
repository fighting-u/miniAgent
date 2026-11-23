from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from src.tools.local_tools import Plan

LOG_PREVIEW_CHARS = 200  # 日志及终端预览保留的最大文本字符数，不含省略号。


@dataclass
class ToolContext:
    """保存工具调用所需的本轮状态，由 Agent 为每个 turn 单独创建。"""

    plan: "Plan"  # 当前 turn 的任务计划。
    cwd: Path  # 当前 turn 的工作目录


@dataclass
class TextContent:
    """一段工具返回的原始文本。"""

    text: str


@dataclass
class ImageContent:
    """带实际 MIME 类型及纯 Base64 数据的图片。"""

    mime_type: str
    data: str


@dataclass
class ToolResult:
    """一次执行产生的有序内容，失败时仅包含错误说明。"""

    content: list[TextContent | ImageContent]
    is_error: bool = False

    def log_output(self) -> str:
        """生成日志及终端预览，合并并截断文本，图片只计数。"""
        texts = [block.text for block in self.content if isinstance(block, TextContent)]
        preview = " ".join(" ".join(texts).split())
        if len(preview) > LOG_PREVIEW_CHARS:
            preview = preview[:LOG_PREVIEW_CHARS] + "…"
        if self.is_error:
            return f"工具执行失败，错误信息：{preview}"
        parts = ["执行成功"]
        images = len(self.content) - len(texts)
        if images:
            parts.append(f"返回 {images} 张图片")
        if preview:
            parts.append(preview)
        return "，".join(parts)


def text_result(text: str, *, is_error: bool = False) -> ToolResult:
    """构造只有一个文本块的成功或失败结果。"""
    return ToolResult([TextContent(text)], is_error=is_error)


@dataclass
class Tool:
    """统一描述工具定义及异步执行入口，不区分本地与 MCP 来源。"""

    name: str  # 提供给模型并用于注册的工具名称。
    description: str  # 提供给模型的工具说明。
    parameters: dict[str, Any]  # 工具参数的 JSON Schema。
    execute: Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]  # 接收模型参数和本轮状态的异步执行入口。
    strict: bool | None = None  # None 表示不向模型请求显式传入 strict。
    supports_parallel: bool = False  # 是否支持与其他允许并发的工具同时执行。
