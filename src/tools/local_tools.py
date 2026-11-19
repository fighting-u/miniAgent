import ast
import json
import logging
from datetime import datetime
from pathlib import Path
import subprocess
from typing import TYPE_CHECKING, Any
from functools import partial

from src.config import Config, PROJECT_ROOT
from src.memory.memory import load_memory
from src.tools.skill import SKILLS_DIR_NAME, SkillLoader
from src.tools.tool import ToolContext

if TYPE_CHECKING:
    from src.tools.tool_manager import ToolManager

# 当前模块使用的日志记录器。
logger = logging.getLogger(__name__)


def resolve_path(path: str, context: ToolContext) -> Path:
    """以本轮工作目录为基准解析路径。"""
    # 这里的 / 是 pathlib.Path 重载的路径拼接运算符，不是简单的字符串拼接。
    # 当右侧是完整的绝对路径时，会直接采用右侧路径，舍弃左侧的工作区。
    return (context.cwd / path).resolve()


def run_get_model_name(*, model_name: str) -> str:
    """读取与当前模型请求相同的统一配置，返回配置的模型名称。"""
    return model_name


def run_get_current_time() -> str:
    """返回系统本地当前时间，使用带时区偏移的 ISO 8601 格式，精确到秒。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# 当前已实现的 shell 工具处理函数，底层仍使用 Windows PowerShell。
def run_powershell(command: str, workdir: str | None = None, *, timeout: int, context: ToolContext) -> str:
    """在本次调用的工作目录执行 PowerShell，并以字符串返回结果。"""
    try:
        cwd = resolve_path(workdir if workdir is not None else ".", context)
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + command,
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return json.dumps({"exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}, ensure_ascii=False)
    except subprocess.TimeoutExpired as error:
        return json.dumps(
            {"error": f"PowerShell 执行超过 {timeout} 秒，已终止。", "stdout": error.stdout or "", "stderr": error.stderr or ""}, ensure_ascii=False
        )
    except ValueError as error:
        return f"错误：{error}"
    except OSError as error:
        return f"无法启动 PowerShell：{error}"


def run_read(path: str, limit: int | None = None, *, context: ToolContext) -> str:
    """读取指定的 UTF-8 文本文件，可选择限制返回的行数。"""
    try:
        lines = resolve_path(path, context).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as error:
        return f"Error: {error}"


def run_write(path: str, content: str, *, context: ToolContext) -> str:
    """向指定的 UTF-8 文本文件写入内容，并按需创建父目录。"""
    try:
        file_path = resolve_path(path, context)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {file_path}"
    except Exception as error:
        return f"Error: {error}"


def run_edit(path: str, old_text: str, new_text: str, *, context: ToolContext) -> str:
    """在指定的 UTF-8 文本文件中替换第一次出现的指定文本。"""
    try:
        file_path = resolve_path(path, context)
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {file_path}"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {file_path}"
    except Exception as error:
        return f"Error: {error}"


def run_glob(pattern: str, workdir: str | None = None, *, context: ToolContext) -> str:
    """从指定目录或默认工作区查找 glob 匹配项，最多返回 200 项。"""
    import glob as glob_module

    try:
        cwd = resolve_path(workdir if workdir is not None else ".", context)
        if not cwd.is_dir():
            logger.warning("glob 工作目录不存在或不是目录：%s", cwd)
            return f"错误：工作目录不存在或不是目录：{cwd}"
        matches: set[str] = set()
        for match in glob_module.glob(pattern, root_dir=cwd, recursive=True):
            # 返回绝对路径，供读写工具直接使用。
            matches.add(str((cwd / match).resolve()))
        shown = sorted(matches)[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except Exception as error:
        return f"Error: {error}"


class Plan:
    """保存、校验并展示当前 turn 的任务计划，按工具批次生成提醒。"""

    # 连续多少批工具未更新计划后，向模型追加一次提醒。
    REMINDER_INTERVAL = 5

    def __init__(self) -> None:
        """初始化空的计划清单和未更新计数。"""
        self.items: list[dict[str, str]] = []
        self._batches_without_update = 0

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
        # 批末会统一加 1，让成功更新计划的这一批以 0 结束。
        self._batches_without_update = -1
        return "计划更新成功"

    def check_reminder(self) -> str | None:
        """在一批工具调用结束后推进计数，达到阈值时返回提醒并清零。"""
        self._batches_without_update += 1
        if self._batches_without_update < self.REMINDER_INTERVAL:
            return None

        self._batches_without_update = 0
        logger.info("连续 %d 批工具调用未更新计划，向模型追加提醒", self.REMINDER_INTERVAL)
        if not self.items:
            return (
                f"系统提醒：当前已连续进行 {self.REMINDER_INTERVAL} 批工具调用，但尚未创建任务计划。"
                "如果当前任务复杂或步骤较多，请使用 update_plan 创建计划。"
            )
        return (
            f"系统提醒：已连续 {self.REMINDER_INTERVAL} 批工具调用未更新任务计划。"
            f"请核对实际进度，如有变化请使用 update_plan 更新计划。\n当前计划：{self.render()}"
        )

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


# 当前 turn 的计划由 Agent 随工具调用传入，供本轮工具使用。
async def run_update_plan(plan: list[Any] | str, *, context: ToolContext) -> str:
    """在事件循环内更新当前 turn 的计划，展示由工具结果事件交给通道完成。"""
    try:
        output = context.plan.update(plan)
    except ValueError as error:
        logger.warning("当前 turn 的计划更新失败：%s", error)
        return f"错误：{error}"
    logger.info("当前 turn 计划已更新")
    return output


def run_load_skill(name: str) -> str:
    """读取技能完整说明，调用时扫描目录以取得当前可用技能。"""
    return SkillLoader(PROJECT_ROOT / SKILLS_DIR_NAME).load(name)


def register_local_tools(registry: "ToolManager", config: Config) -> None:
    """集中注册本地工具，并绑定各工具需要的运行时配置。"""
    # 提供给模型的工具定义；parameters 使用 JSON Schema 描述模型可传入的参数。
    registry.register_local_tool(
        name="powershell",
        func=partial(run_powershell, timeout=config.powershell_timeout),
        description="执行 PowerShell 命令。",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "workdir": {"type": "string", "description": "执行命令的目录，可用绝对路径或相对于本轮当前目录的路径，省略时使用本轮当前目录。"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        strict=False,
        uses_context=True,
    )

    registry.register_local_tool(
        name="read_file",
        func=run_read,
        description="Read file contents.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        uses_context=True,
    )

    registry.register_local_tool(
        name="write_file",
        func=run_write,
        description="Write content to a file.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        uses_context=True,
    )

    registry.register_local_tool(
        name="edit_file",
        func=run_edit,
        description="Replace exact text in a file once.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
        uses_context=True,
    )

    registry.register_local_tool(
        name="glob",
        func=run_glob,
        description="查找与 glob 模式匹配的文件或目录，支持 ** 递归匹配。",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "workdir": {"type": "string", "description": "搜索起点，可用绝对路径或相对于本轮当前目录的路径，省略时使用本轮当前目录。"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        strict=False,
        uses_context=True,
    )

    registry.register_local_tool(
        name="update_plan",
        func=run_update_plan,
        description="创建或更新任务计划，适合长程任务或包含多条指令的请求。",
        parameters={
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
        strict=True,
        uses_context=True,
    )

    registry.register_local_tool(
        name="load_skill",
        func=run_load_skill,
        description="按名称读取某项技能的完整说明。仅在技能适用于当前任务时调用。",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "系统提示词中列出的技能名称。"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        strict=True,
    )

    registry.register_local_tool(
        name="load_memory",
        func=load_memory,
        description="根据系统提示词中的记忆文件名，按需读取一条长期记忆的完整内容。",
        parameters={
            "type": "object",
            "properties": {"filename": {"type": "string", "description": "长期记忆目录中列出的 Markdown 文件名。"}},
            "required": ["filename"],
            "additionalProperties": False,
        },
        strict=True,
    )

    registry.register_local_tool(
        name="get_model_name",
        func=partial(run_get_model_name, model_name=config.model),
        description="获取当前请求配置的模型名称，读取统一运行配置。",
        parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        strict=True,
    )

    registry.register_local_tool(
        name="get_current_time",
        func=run_get_current_time,
        description="获取系统本地当前时间，以带时区偏移的 ISO 8601 格式返回，精确到秒，例如 2026-11-11T15:30:45+08:00。",
        parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        strict=True,
    )
