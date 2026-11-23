import logging
from pathlib import Path
from typing import Any, Callable

from src.common.common import truncate_middle_to_budget
from src.compact.estimator import estimate_text
from src.config import Config, PROJECT_ROOT
from src.tools.tool import TextContent, ToolContext, ToolResult

# 当前模块使用的日志记录器。
logger = logging.getLogger(__name__)

# 超长工具输出完整内容的转存目录名，固定位于项目根目录下，不随 cwd 变化。
TASK_OUTPUT_DIR_NAME = ".task_outputs"


class HookManager:
    """管理 Hook 的注册与触发，持有运行配置，供需要配置的 Hook 使用。"""

    def __init__(self, config: Config) -> None:
        """保存配置，创建事件注册表并注册当前已有的 Hook 扩展逻辑。"""
        self._config = config
        # Hook 事件注册表，用于在核心流程的固定位置挂载外部扩展逻辑。
        self._hooks: dict[str, list[Callable[..., Any]]] = {"PreUserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "PostToolBatch": [], "Stop": []}
        # 注册顺序就是同一事件的执行顺序；大输出日志排在截断前，记录原始大小。
        self.register("PreUserPromptSubmit", workdir_log_hook)
        self.register("PreToolUse", tool_request_log_hook)
        self.register("PostToolUse", large_output_hook)
        self.register("PostToolUse", self.truncate_output_hook)
        self.register("PostToolBatch", plan_reminder_hook)
        self.register("Stop", summary_hook)

    def trigger(self, event: str, *args: Any) -> Any | None:
        """依次触发指定事件的 Hook，任一 Hook 返回非 None 值时立即返回该值。"""
        for callback in self._hooks[event]:
            result = callback(*args)
            if result is not None:
                return result
        return None

    def register(self, event: str, callback: Callable[..., Any]) -> None:
        """向指定事件注册一个 Hook 回调函数。"""
        self._hooks[event].append(callback)

    def truncate_output_hook(self, tool_call: Any, output: ToolResult) -> None:
        """文本块超过配置的 token 上限时，完整内容转存到临时文件，并就地替换为首尾保留的截断文本。"""
        max_tokens = self._config.tool_output_max_tokens
        for index, block in enumerate(output.content):
            if not isinstance(block, TextContent):
                continue
            original_tokens = estimate_text(block.text)
            if original_tokens <= max_tokens:
                continue
            output_dir = PROJECT_ROOT / TASK_OUTPUT_DIR_NAME
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"{tool_call.call_id}_{index}.txt"
            output_path.write_text(block.text, encoding="utf-8")
            truncated = truncate_middle_to_budget(block.text, max_tokens)
            block.text = (
                f"{truncated}\n[输出过长已截断，完整内容已保存到 {output_path}，"
                f"请用 PowerShell 的 Select-String 或 Get-Content 配合 -Skip/-First 分段查看]"
            )
            logger.info("[Hook] %s 输出约 %d token，超过上限 %d，已截断，完整内容保存到：%s", tool_call.name, original_tokens, max_tokens, output_path)
        return None


def workdir_log_hook(cwd: Path) -> None:
    """在读取用户输入前输出当前工作目录。"""
    logger.info("[Hook] 当前工作目录：%s", cwd)
    return None


def tool_request_log_hook(tool_call: Any, arguments: dict[str, Any]) -> None:
    """在工具执行前输出模型请求的工具名称和参数预览。"""
    arguments_preview = str(list(arguments.values())[:2])[:60]
    logger.info("[Hook] 模型请求调用工具：%s(%s)", tool_call.name, arguments_preview)
    return None


def large_output_hook(tool_call: Any, output: ToolResult) -> None:
    """在工具输出超过十万个字符时输出提醒日志。"""
    size = sum(len(block.text) if isinstance(block, TextContent) else len(block.data) for block in output.content)
    if size > 100_000:
        logger.warning("[Hook] %s 输出较大，共 %d 个字符", tool_call.name, size)
    return None


def plan_reminder_hook(context: ToolContext) -> str | None:
    """在整批工具调用结束后检查是否需要提醒模型更新计划。"""
    return context.plan.check_reminder()


def summary_hook(messages: list[Any]) -> None:
    """从最后一条用户消息开始，统计本轮产生的工具调用结果数量。"""
    last_user_index = next(
        (index for index in range(len(messages) - 1, -1, -1) if isinstance(messages[index], dict) and messages[index].get("role") == "user"), -1
    )
    tool_count = sum(1 for message in messages[last_user_index + 1 :] if isinstance(message, dict) and message.get("type") == "function_call_output")
    logger.info("[Hook] 本轮请求共产生 %d 个工具结果", tool_count)
    return None
