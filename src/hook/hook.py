import logging
from pathlib import Path
from typing import Any, Callable

from src.tools.tool import ToolContext

# 当前模块使用的日志记录器。
logger = logging.getLogger(__name__)

# Hook 事件注册表，用于在核心流程的固定位置挂载外部扩展逻辑。
HOOKS: dict[str, list[Callable[..., Any]]] = {"PreUserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "PostToolBatch": [], "Stop": []}


def trigger_hooks(event: str, *args: Any) -> Any | None:
    """依次触发指定事件的 Hook，任一 Hook 返回非 None 值时立即返回该值。"""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


def register_hook(event: str, callback: Callable[..., Any]) -> None:
    """向指定事件注册一个 Hook 回调函数。"""
    HOOKS[event].append(callback)


def workdir_log_hook(cwd: Path) -> None:
    """在读取用户输入前输出当前工作目录。"""
    logger.info("[Hook] 当前工作目录：%s", cwd)
    return None


def tool_request_log_hook(tool_call: Any, arguments: dict[str, Any]) -> None:
    """在工具执行前输出模型请求的工具名称和参数预览。"""
    arguments_preview = str(list(arguments.values())[:2])[:60]
    logger.info("[Hook] 模型请求调用工具：%s(%s)", tool_call.name, arguments_preview)
    return None


def tool_output_log_hook(tool_call: Any, output: str) -> None:
    """在工具执行后输出工具结果的前 200 个字符。"""
    logger.info("[Hook] %s 输出预览：%s", tool_call.name, output[:200])
    return None


def large_output_hook(tool_call: Any, output: str) -> None:
    """在工具输出超过十万个字符时输出提醒日志。"""
    if len(output) > 100_000:
        logger.warning("[Hook] %s 输出较大，共 %d 个字符", tool_call.name, len(output))
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


# 注册当前已有的 Hook 扩展逻辑，注册顺序就是同一事件的执行顺序。
register_hook("PreUserPromptSubmit", workdir_log_hook)
register_hook("PreToolUse", tool_request_log_hook)
register_hook("PostToolUse", tool_output_log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("PostToolBatch", plan_reminder_hook)
register_hook("Stop", summary_hook)
