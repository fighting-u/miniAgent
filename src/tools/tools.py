import ast
import json
import logging
from datetime import datetime
from pathlib import Path
import subprocess
from typing import Any
from copy import deepcopy
from functools import partial

from src.config import Config, PROJECT_ROOT
from src.memory.memory import load_memory
from src.tools.skill import SKILLS_DIR_NAME, SkillLoader

# 当前模块使用的日志记录器。
logger = logging.getLogger(__name__)

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
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"], "description": "计划项状态。"},
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
    {
        "type": "function",
        "name": "load_skill",
        "description": "按名称读取某项技能的完整说明。仅在技能适用于当前任务时调用。",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "系统提示词中列出的技能名称。"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "load_memory",
        "description": "根据系统提示词中的记忆文件名，按需读取一条长期记忆的完整内容。",
        "parameters": {
            "type": "object",
            "properties": {"filename": {"type": "string", "description": "长期记忆目录中列出的 Markdown 文件名。"}},
            "required": ["filename"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "compact",
        "description": "当较早对话已占用较多上下文并且细节不再重要时，可主动归档并摘要历史以释放空间。",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_model_name",
        "description": "获取当前请求配置的模型名称，读取统一运行配置。",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_current_time",
        "description": "获取系统本地当前时间，以带时区偏移的 ISO 8601 格式返回，精确到秒，例如 2026-11-11T15:30:45+08:00。",
        "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
]


def run_get_model_name(*, config: Config) -> str:
    """读取与当前模型请求相同的统一配置，返回配置的模型名称。"""
    model_name = config.model
    if not model_name.strip():
        logger.warning("获取模型名称失败：config.model 未配置或为空")
        return "错误：config.model 未配置或为空。"
    return model_name


def run_get_current_time() -> str:
    """返回系统本地当前时间，使用带时区偏移的 ISO 8601 格式，精确到秒。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# 当前已实现的 shell 工具处理函数，底层仍使用 Windows PowerShell。
def run_powershell(command: str, *, config: Config) -> str:
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
            cwd=config.workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.powershell_timeout,
            check=False,
        )
        return json.dumps({"exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}, ensure_ascii=False)
    except subprocess.TimeoutExpired as error:
        return json.dumps(
            {"error": f"PowerShell 执行超过 {config.powershell_timeout} 秒，已终止。", "stdout": error.stdout or "", "stderr": error.stderr or ""},
            ensure_ascii=False,
        )
    except OSError as error:
        return f"无法启动 PowerShell：{error}"


def is_allowed_path(path: Path, config: Config) -> bool:
    """检查解析后的路径是否属于当前工作区或额外允许目录。"""
    return any(path.is_relative_to(root) for root in (config.workdir, *config.extra_allowed_dirs))


def safe_path(path: str, config: Config) -> Path:
    """将相对路径解析到工作区，阻止访问所有允许目录之外的路径。"""
    # 这里的 / 是 pathlib.Path 重载的路径拼接运算符，不是简单的字符串拼接。
    # 当右侧是完整的绝对路径时，会直接采用右侧路径，舍弃左侧的工作区。
    resolved_path = (config.workdir / path).resolve()
    if not is_allowed_path(resolved_path, config):
        logger.warning("文件路径不在允许范围内，拒绝访问：%s", resolved_path)
        raise ValueError(f"路径不在允许的工作区或额外目录内：{path}")
    return resolved_path


def run_read(path: str, limit: int | None = None, *, config: Config) -> str:
    """读取工作区内的 UTF-8 文本文件，可选择限制返回的行数。"""
    try:
        lines = safe_path(path, config).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as error:
        return f"Error: {error}"


def run_write(path: str, content: str, *, config: Config) -> str:
    """向工作区内的 UTF-8 文本文件写入内容，并按需创建父目录。"""
    try:
        file_path = safe_path(path, config)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as error:
        return f"Error: {error}"


def run_edit(path: str, old_text: str, new_text: str, *, config: Config) -> str:
    """在工作区内的 UTF-8 文本文件中替换第一次出现的指定文本。"""
    try:
        file_path = safe_path(path, config)
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as error:
        return f"Error: {error}"


def run_glob(pattern: str, *, config: Config) -> str:
    """在工作区内查找匹配 glob 模式的文件或目录，最多返回 200 项。"""
    import glob as glob_module

    try:
        matches = sorted(
            {match for match in glob_module.glob(pattern, root_dir=config.workdir, recursive=True)
             if is_allowed_path((config.workdir / match).resolve(), config)}
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
            marker = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}[item["status"]]
            lines.append(f"{marker} {item['content']}")

        completed_count = sum(item["status"] == "completed" for item in self.items)
        lines.append(f"\n（已完成 {completed_count}/{len(self.items)}）")
        return "\n".join(lines)


# 当前 Agent 任务的计划管理器由会话持有，并在工具调用时显式传入。
def run_update_plan(plan: list[Any] | str, *, manager: PlanManager) -> str:
    """更新当前会话计划，展示由工具结果事件交给通道完成。"""
    try:
        output = manager.update(plan)
    except ValueError as error:
        return f"错误：{error}"
    logger.info("当前会话计划已更新")
    return output

# todo:?
def build_tool_handlers(config: Config, plan: PlanManager) -> dict[str, Any]:
    """把当前配置和会话计划绑定到工具分发表，不再依赖全局工作区或技能实例。"""
    return {
        "get_model_name": partial(run_get_model_name, config=config),
        "get_current_time": run_get_current_time,
        "powershell": partial(run_powershell, config=config),
        "read_file": partial(run_read, config=config),
        "write_file": partial(run_write, config=config),
        "edit_file": partial(run_edit, config=config),
        "glob": partial(run_glob, config=config),
        "update_plan": partial(run_update_plan, manager=plan),
        "load_skill": SkillLoader(PROJECT_ROOT / SKILLS_DIR_NAME).load,
        "load_memory": load_memory,
    }


def assemble_tools(mcp_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """组合内置与 MCP 工具，检查重名并返回独立列表，不修改全局定义。"""
    combined = deepcopy([*TOOLS, *mcp_tools])
    names = [tool["name"] for tool in combined]
    # ？怎么命名的
    if len(names) != len(set(names)):
        raise ValueError("内置工具与 MCP 工具名称冲突")
    return combined
