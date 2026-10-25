import ast
import json
import os
from pathlib import Path
import subprocess
from typing import Any


# PowerShell 单次执行的最长时间，单位为秒。
POWERSHELL_TIMEOUT_SECONDS = 120

# 文件工具允许访问的工作区根目录，使用程序启动时的当前目录。
WORKDIR = Path.cwd().resolve()

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
    {
        "type": "function",
        "name": "update_plan",
        "description": "创建或更新任务计划，适合长程任务或包含多条指令的请求。",
        "parameters": {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "array",
                    "description": "完整的计划清单，每次调用都会覆盖现有计划。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "计划项内容。"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "计划项状态。",
                            },
                        },
                        "required": ["content", "status"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["plan"],
            "additionalProperties": False,
        },
        "strict": True,
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


class PlanManager:
    """保存、校验并展示当前任务计划。"""

    def __init__(self) -> None:
        """初始化空的计划清单。"""
        self.items: list[dict[str, str]] = []

    def update(self, plan: list[Any] | str) -> str:
        """校验并整体替换当前计划，然后返回渲染后的计划文本。"""
        if isinstance(plan, str):
            try:
                plan = json.loads(plan)
            except json.JSONDecodeError:
                try:
                    plan = ast.literal_eval(plan)
                except (SyntaxError, ValueError) as error:
                    raise ValueError("计划必须是列表或 JSON 数组字符串。") from error

        if not isinstance(plan, list):
            raise ValueError("计划必须是列表。")
        if len(plan) > 20:
            raise ValueError("计划项最多允许 20 条。")

        validated: list[dict[str, str]] = []
        in_progress_count = 0
        for index, item in enumerate(plan):
            if not isinstance(item, dict):
                raise ValueError(f"plan[{index}] 必须是对象。")

            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).lower()
            if not content:
                raise ValueError(f"plan[{index}] 缺少 content。")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"plan[{index}] 的状态 '{status}' 无效。")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({"content": content, "status": status})

        if in_progress_count > 1:
            raise ValueError("同一时间只能有一条计划处于 in_progress 状态。")

        self.items = validated
        return self.render()

    def render(self) -> str:
        """把当前计划渲染为便于模型和终端阅读的文本。"""
        if not self.items:
            return "暂无计划。"

        lines = []
        for item in self.items:
            marker = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed": "[x]",
            }[item["status"]]
            lines.append(f"{marker} {item['content']}")

        completed_count = sum(item["status"] == "completed" for item in self.items)
        lines.append(f"\n（已完成 {completed_count}/{len(self.items)}）")
        return "\n".join(lines)


# 当前 Agent 任务共享的计划管理器。
PLAN = PlanManager()


def run_update_plan(plan: list[Any] | str) -> str:
    """执行计划更新工具，并在终端中展示更新后的计划。"""
    try:
        output = PLAN.update(plan)
    except ValueError as error:
        return f"错误：{error}"
    print(f"\n\033[33m## 当前计划\033[0m\n{output}")
    return output


# 工具名称到具体处理函数的分发表。
TOOL_HANDLERS = {
    "powershell": run_powershell,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "update_plan": run_update_plan,
}
