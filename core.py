import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable

from dotenv import load_dotenv
from openai import OpenAI

# 系统提示词
INSTRUCTIONS = """你是一个运行在 Windows 环境中的智能体助手，帮助用户完成任务。"""

# PowerShell 单次执行的最长时间，单位为秒。
POWERSHELL_TIMEOUT_SECONDS = 120

# 文件工具允许访问的工作区根目录，使用程序启动时的当前目录。
WORKDIR = Path.cwd().resolve()

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

# 提供给模型的工具定义；parameters 使用 JSON Schema 描述模型可传入的参数。
TOOLS = [
    {
        "type": "function",
        "name": "powershell",
        "description": "Run a PowerShell command.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "read_file",
        "description": "Read file contents.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "Write content to a file.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "edit_file",
        "description": "Replace exact text in a file once.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "glob",
        "description": "Find files matching a glob pattern; ** matches recursively.",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"], "additionalProperties": False},
    },
]


# 当前已实现的 shell 工具处理函数，底层仍使用 Windows PowerShell。
def run_powershell(command: str) -> str:
    """在当前项目目录执行 PowerShell 命令，并以字符串返回执行结果。"""
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + command,
            ],
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=POWERSHELL_TIMEOUT_SECONDS,
            check=False,
        )
        return json.dumps({"exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}, ensure_ascii=False)
    except subprocess.TimeoutExpired as error:
        return json.dumps(
            {"error": f"PowerShell 执行超过 {POWERSHELL_TIMEOUT_SECONDS} 秒，已终止。", "stdout": error.stdout or "", "stderr": error.stderr or ""},
            ensure_ascii=False,
        )
    except OSError as error:
        return f"无法启动 PowerShell：{error}"


def safe_path(path: str) -> Path:
    """将相对路径解析到工作区内，并阻止访问工作区之外的路径。"""
    resolved_path = (WORKDIR / path).resolve()
    if not resolved_path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {path}")
    return resolved_path


def run_read(path: str, limit: int | None = None) -> str:
    """读取工作区内的 UTF-8 文本文件，可选择限制返回的行数。"""
    try:
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as error:
        return f"Error: {error}"


def run_write(path: str, content: str) -> str:
    """向工作区内的 UTF-8 文本文件写入内容，并按需创建父目录。"""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as error:
        return f"Error: {error}"


def run_edit(path: str, old_text: str, new_text: str) -> str:
    """在工作区内的 UTF-8 文本文件中替换第一次出现的指定文本。"""
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as error:
        return f"Error: {error}"


def run_glob(pattern: str) -> str:
    """在工作区内查找匹配 glob 模式的文件或目录，最多返回 200 项。"""
    import glob as glob_module

    try:
        matches = sorted(
            {
                match
                for match in glob_module.glob(pattern, root_dir=WORKDIR, recursive=True)
                if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
            }
        )
        shown = matches[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except Exception as error:
        return f"Error: {error}"


# 工具名称到具体处理函数的分发表。
TOOL_HANDLERS = {
    "powershell": run_powershell,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}

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


def agent_loop(client: OpenAI, messages: list[Any]) -> None:
    """持续请求模型并执行工具，直到模型返回最终文本。"""
    while True:
        # 发送当前对话，请求模型继续推理或调用工具。
        response = client.responses.create(model=os.getenv("MODEL"), instructions=INSTRUCTIONS, input=messages, tools=TOOLS)
        messages.extend(response.output)

        # 从模型本轮输出中筛选所有函数工具调用。
        tool_calls = [item for item in response.output if item.type == "function_call"]

        # 没有工具调用时，模型已经给出最终答案，本次 Agent 循环结束。
        if not tool_calls:
            forced_input = trigger_hooks("Stop", messages)
            # todo: 后续优化点，不简单以不调用工具为结束标志
            if forced_input is not None:
                messages.append({"role": "user", "content": forced_input})
                continue
            return

        # 依次分发模型请求的工具调用，并收集 OpenAI 格式的字符串结果。
        results = []
        for tool_call in tool_calls:
            try:
                # 这里是因为claude返回就是python字典，而openAI是json，要转换下
                arguments = json.loads(tool_call.arguments)

                # 工具执行前触发 Hook，返回非 None 值时跳过本次工具调用。
                blocked = trigger_hooks("PreToolUse", tool_call, arguments)
                if blocked is not None:
                    # todo: 这里给llm的反馈太笼统了，后续可优化细分
                    results.append(
                        {"type": "function_call_output", "call_id": tool_call.call_id, "output": str(blocked)}
                    )
                    continue

                handler = TOOL_HANDLERS.get(tool_call.name)
                output = str(handler(**arguments)) if handler else f"未知工具: {tool_call.name}"
                trigger_hooks("PostToolUse", tool_call, output)
            except (json.JSONDecodeError, TypeError) as error:
                output = f"工具参数无效：{error}"

            results.append({"type": "function_call_output", "call_id": tool_call.call_id, "output": output})
        messages.extend(results)


def main() -> None:
    """加载配置，读取用户运行时输入并启动一次 Agent 任务。"""
    load_dotenv()
    base_url = os.getenv("BASE_URL")
    # api_key = os.getenv("OPENAI_API_KEY")
    # qwen
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("未找到 API_KEY，请先在 .env 中配置。")

    client = OpenAI(api_key=api_key, base_url=base_url)

    print("请输入问题或者任务，enter提交，输入q结束对话。")
    history = []

    while True:
        # 在读取本轮用户输入前触发 Hook，便于扩展输入提示前的行为。
        trigger_hooks("PreUserPromptSubmit")
        user_request = input("请输入：").strip()
        if user_request.lower() == "q":
            print("结束对话。")
            break

        history.append({"role": "user", "content": user_request})
        agent_loop(client, history)

        # 打印模型最后一次返回的文本内容。
        response_content = history[-1].content
        if isinstance(response_content, list):
            for block in response_content:
                if getattr(block, "type", None) == "output_text":
                    print(block.text)
        print()


if __name__ == "__main__":
    main()
