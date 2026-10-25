import re
from typing import Any, Callable

from tools import WORKDIR


# 第一重闸门的 PowerShell 高危命令片段，命中后直接拒绝执行。
DENY_LIST = [
    "remove-item -recurse -force c:\\",
    "remove-item -force -recurse c:\\",
    "format-volume",
    "clear-disk",
    "initialize-disk",
    "stop-computer",
    "restart-computer",
    "shutdown /s",
    "shutdown /r",
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


# 第二重闸门的敏感操作规则，命中后需要进入用户审批流程。
PERMISSION_RULES = [
    {
        "tools": ["read_file", "write_file", "edit_file"],
        "check": lambda args: not (WORKDIR / str(args.get("path", ""))).resolve().is_relative_to(WORKDIR),
        "message": "访问工作区外的文件",
    },
    {
        "tools": ["powershell"],
        "check": lambda args: contains_destructive_command(str(args.get("command", "")))
        or any(
            keyword in str(args.get("command", "")).casefold()
            for keyword in POWERSHELL_SENSITIVE_KEYWORDS
        ),
        "message": "潜在的破坏性 PowerShell 命令",
    },
]


def check_deny_list(command: str) -> str | None:
    """检查 PowerShell 命令是否命中硬拒绝名单。"""
    normalized_command = command.casefold()
    for pattern in DENY_LIST:
        if pattern in normalized_command:
            return f"Blocked: '{pattern}' is on the deny list"
    return None


def check_rules(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """按照权限规则检查工具调用是否需要用户审批。"""
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](tool_input):
            return rule["message"]
    return None


def ask_user(tool_name: str, tool_input: dict[str, Any], reason: str) -> str:
    """向用户展示敏感操作信息，并请求明确批准或拒绝。"""
    print(f"\n⚠  {reason}")
    print(f"   Tool: {tool_name}({tool_input})")
    choice = input("   是否允许? [y/N] ").strip().lower()
    return "allow" if choice in ("y", "yes") else "deny"


def check_permission(tool_name: str, tool_input: dict[str, Any]) -> bool:
    """依次经过硬拒绝、敏感操作识别和用户审批三道权限闸门。"""
    # 闸门 1：命中高危规则时直接拒绝。
    if tool_name == "powershell":
        reason = check_deny_list(str(tool_input.get("command", "")))
        if reason:
            print(f"\n⛔ {reason}")
            return False

    # 闸门 2 和 3：识别敏感操作，并交给用户审批。
    reason = check_rules(tool_name, tool_input)
    if reason:
        decision = ask_user(tool_name, tool_input, reason)
        if decision == "deny":
            return False

    return True


def permission_hook(tool_call: Any, arguments: dict[str, Any]) -> str | None:
    """将现有权限审查接入工具执行前 Hook，拒绝时返回给模型的中文反馈。"""
    if not check_permission(tool_call.name, arguments):
        return "权限被拒绝。"
    return None


def workdir_log_hook() -> None:
    """在读取用户输入前输出当前工作目录。"""
    print(f"\033[90m[Hook] 当前工作目录：{WORKDIR}\033[0m")
    return None


def tool_request_log_hook(tool_call: Any, arguments: dict[str, Any]) -> None:
    """在工具执行前输出模型请求的工具名称和参数预览。"""
    arguments_preview = str(list(arguments.values())[:2])[:60]
    # \033[33m 是 ANSI 转义码，用于在终端中显示黄色文本，\033[0m 用于重置颜色。
    print(f"\033[33m[Hook] 模型请求调用工具：{tool_call.name}({arguments_preview})\033[0m")
    return None


def tool_output_log_hook(tool_call: Any, output: str) -> None:
    """在工具执行后输出工具结果的前 200 个字符。"""
    print(f"\033[90m[Hook] {tool_call.name} 输出预览：{output[:200]}\033[0m")
    return None


def large_output_hook(tool_call: Any, output: str) -> None:
    """在工具输出超过十万个字符时输出提醒日志。"""
    if len(output) > 100_000:
        print(f"\033[33m[Hook] {tool_call.name} 输出较大，共 {len(output)} 个字符\033[0m")
    return None


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
    print(f"\033[90m[Hook] 本轮请求共执行 {tool_count} 个工具\033[0m")
    return None


# 注册当前已有的 Hook 扩展逻辑，注册顺序就是同一事件的执行顺序。
register_hook("PreUserPromptSubmit", workdir_log_hook)
register_hook("PreToolUse", tool_request_log_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PostToolUse", tool_output_log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)
