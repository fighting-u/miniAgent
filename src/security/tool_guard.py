from dataclasses import dataclass
from enum import Enum
from typing import Any

from src.config import Config
from src.tools.local_tools import resolve_path
from src.tools.tool import ToolContext


class ToolDecision(str, Enum):
    """工具请求的审计结论。"""

    ALLOW = "allow"  # 通过检查，可以执行。
    DENY = "deny"  # 命中硬拒绝规则。
    REQUIRE_APPROVAL = "require_approval"  # 命中需要用户审批的规则。


@dataclass(frozen=True)
class ToolAuditResult:
    """汇总安全检查的结论和反馈。"""

    decision: ToolDecision  # 工具请求的审计结论。
    feedback: str  # 拒绝或需要审批的原因，通过时为空。


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

# PowerShell 命令同时包含这组关键词时，需要审批。
POWERSHELL_APPROVAL_KEYWORDS = ("remove-item", "-recurse")

def check_powershell_sensitive_command(command: str) -> bool:
    """忽略大小写判断 PowerShell 命令是否同时包含删除和递归关键词。"""
    normalized_command = command.casefold()
    return all(keyword in normalized_command for keyword in POWERSHELL_APPROVAL_KEYWORDS)


def check_powershell_deny_command(command: str) -> str | None:
    """忽略大小写检查命令是否包含某条硬拒绝规则的全部关键词。"""
    normalized_command = command.casefold()
    for keywords in DENY_LIST:
        if all(keyword in normalized_command for keyword in keywords):
            return "系统消息：该命令是高危命令，不允许执行，请检查命令合理性或向用户反馈。"
    return None


def is_outside_allowed_dirs(path: str | None, config: Config, context: ToolContext) -> bool:
    """以本轮工作目录解析指定路径，判断它是否在所有允许目录之外。"""
    resolved_path = resolve_path(path if path is not None else ".", context)
    return not any(resolved_path.is_relative_to(root) for root in (context.cwd, *config.allowed_dirs))


# 第二重闸门的敏感操作规则，命中后需要进入用户审批流程。
PERMISSION_RULES = [
    {
        "tools": ["read_file", "write_file", "edit_file"],
        "check": lambda args, config, context: is_outside_allowed_dirs(args.get("path"), config, context),
        "message": "访问允许的目录之外的文件夹或文件",
    },
    {
        "tools": ["powershell", "glob"],
        "check": lambda args, config, context: is_outside_allowed_dirs(args.get("workdir"), config, context),
        "message": "工作目录在允许范围之外",
    },
    {
        "tools": ["powershell"],
        "check": lambda args, _config, _context: check_powershell_sensitive_command(str(args.get("command", ""))),
        "message": "PowerShell 递归删除操作",
    },
]


def check_approval_required(tool_name: str, tool_args: dict[str, Any], config: Config, context: ToolContext) -> str | None:
    """按照权限规则检查工具调用是否需要用户审批。"""
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](tool_args, config, context):
            return rule["message"]
    return None


def audit_tool_request(tool_name: str, tool_args: dict[str, Any], config: Config, context: ToolContext) -> ToolAuditResult:
    """依次检查硬拒绝和审批规则，命中后立即返回对应结论。"""
    if tool_name == "powershell":
        denied = check_powershell_deny_command(str(tool_args.get("command", "")))
        if denied is not None:
            return ToolAuditResult(ToolDecision.DENY, denied)
    reason = check_approval_required(tool_name, tool_args, config, context)
    if reason is not None:
        return ToolAuditResult(ToolDecision.REQUIRE_APPROVAL, reason)
    return ToolAuditResult(ToolDecision.ALLOW, "")
