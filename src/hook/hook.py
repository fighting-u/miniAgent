import logging
import re
from typing import Any, Callable

from src.config import Config
from src.tools.tool import ToolContext


# 当前模块使用的日志记录器。
logger = logging.getLogger(__name__)

# 第一重闸门的 PowerShell 高危命令关键词组，同组关键词全部命中时直接拒绝执行。
DENY_LIST = [
    ("remove-item", "c:\\"),
    ("format-volume",),
    ("clear-disk",),
    ("initialize-disk",),
    ("stop-computer",),
    ("restart-computer",),
    ("shutdown"),
]

# 用于识别 PowerShell 删除命令及其常用别名的正则表达式。
DESTRUCTIVE_COMMAND_WORD = re.compile(
    r"(?i)(?:^|[;&|()\r\n])\s*(?:remove-item|del|erase|rd|rmdir|clear-content)(?=\s|$|[;&|()])"
)

# 需要用户审批的 PowerShell 敏感操作关键词，匹配时忽略大小写。
POWERSHELL_SENSITIVE_KEYWORDS = [
    "set-content ",
    "out-file ",
    "set-acl ",
    "icacls ",
    "takeown ",
]

# Hook 事件注册表，用于在核心流程的固定位置挂载外部扩展逻辑。
HOOKS: dict[str, list[Callable[..., Any]]] = {
    "PreUserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "PostToolBatch": [],
    "Stop": [],
}


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


def contains_destructive_command(command: str) -> bool:
    """判断 PowerShell 命令中是否包含删除或清空内容的操作。"""
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))


def is_outside_allowed_dirs(args: dict[str, Any], config: Config) -> bool:
    """判断文件工具的目标路径是否在所有允许目录之外。"""
    path = (config.workdir / str(args.get("path", ""))).resolve()
    return not any(path.is_relative_to(root) for root in (config.workdir, *config.extra_allowed_dirs))


# 第二重闸门的敏感操作规则，命中后需要进入用户审批流程。
PERMISSION_RULES = [
    {
        "tools": ["read_file", "write_file", "edit_file"],
        "check": is_outside_allowed_dirs,
        "message": "访问允许的目录之外的文件夹或文件",
    },
    {
        "tools": ["powershell"],
        "check": lambda args, config: contains_destructive_command(str(args.get("command", "")))
        or any(
            keyword in str(args.get("command", "")).casefold()
            for keyword in POWERSHELL_SENSITIVE_KEYWORDS
        ),
        "message": "潜在的破坏性 PowerShell 命令",
    },
]


def check_powershell_deny_command(command: str) -> str | None:
    """忽略大小写检查命令是否包含某条硬拒绝规则的全部关键词。"""
    normalized_command = command.casefold()
    for keywords in DENY_LIST:
        if all(keyword in normalized_command for keyword in keywords):
            return f"系统消息：该命令是高危命令，不允许执行，请检查命令合理性或向用户反馈。"
    return None


def check_approval_required(tool_name: str, tool_input: dict[str, Any], config: Config) -> str | None:
    """按照权限规则检查工具调用是否需要用户审批。"""
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](tool_input, config):
            return rule["message"]
    return None


# 权限闸门保留以上规则；交互审批由 Agent 事件和 Runtime 审批代理完成。

def workdir_log_hook(config: Config) -> None:
    """在读取用户输入前输出当前工作目录。"""
    logger.info("[Hook] 当前工作目录：%s", config.workdir)
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
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if isinstance(messages[index], dict) and messages[index].get("role") == "user"
        ),
        -1,
    )
    tool_count = sum(
        1
        for message in messages[last_user_index + 1 :]
        if isinstance(message, dict) and message.get("type") == "function_call_output"
    )
    logger.info("[Hook] 本轮请求共产生 %d 个工具结果", tool_count)
    return None


# 注册当前已有的 Hook 扩展逻辑，注册顺序就是同一事件的执行顺序。
register_hook("PreUserPromptSubmit", workdir_log_hook)
register_hook("PreToolUse", tool_request_log_hook)
register_hook("PostToolUse", tool_output_log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("PostToolBatch", plan_reminder_hook)
register_hook("Stop", summary_hook)
