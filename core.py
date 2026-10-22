import json
import os
from pathlib import Path
from pyexpat.errors import messages
import subprocess
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

# 系统提示词
INSTRUCTIONS = """你是一个运行在 Windows 环境中的智能体助手，帮助用户完成任务。"""

# PowerShell 单次执行的最长时间，单位为秒。
POWERSHELL_TIMEOUT_SECONDS = 120

# 文件工具允许访问的工作区根目录，使用程序启动时的当前目录。
WORKDIR = Path.cwd().resolve()

# 提供给模型的工具定义；parameters 使用 JSON Schema 描述模型可传入的参数。
TOOLS = [
    {
        "type": "function",
        "name": "bash",
        "description": "Run a shell command.",
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
def run_bash(command: str) -> str:
    """在当前项目目录执行 PowerShell 命令，并以字符串返回执行结果。"""
    # 用小写关键词拦截常见的递归删除、磁盘格式化以及关机重启命令。
    dangerous_commands = ["remove-item -recurse", "format-volume", "clear-disk", "stop-computer", "restart-computer", "shutdown", "reboot"]
    normalized_command = command.lower()
    if any(item in normalized_command for item in dangerous_commands):
        return "检测到危险 PowerShell 命令，已阻止执行。"

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
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
}


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
            return

        # 依次分发模型请求的工具调用，并收集 OpenAI 格式的字符串结果。
        results = []
        for tool_call in tool_calls:
            try:
                # 这里是因为claude返回就是python字典，而openAI是json，要转换下
                arguments = json.loads(tool_call.arguments)
                # \033[33m 是 ANSI 转义码，用于在终端中显示黄色文本，\033[0m 用于重置颜色。
                print(f"\033[33m> Agent请求调用工具：{tool_call.name}\033[0m")
                handler = TOOL_HANDLERS.get(tool_call.name)
                output = str(handler(**arguments)) if handler else f"未知工具: {tool_call.name}"
                print(output[:200])
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
