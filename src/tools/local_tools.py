import ast
import base64
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
import subprocess
from typing import TYPE_CHECKING, Any
from functools import partial

from src.config import Config, PROJECT_ROOT
from src.prompt.developer import CREATE_PLAN_REMINDER, UPDATE_PLAN_REMINDER
from src.memory.memory import load_memory
from src.tools.skill import SKILLS_DIR_NAME, SkillLoader
from src.tools.tool import ImageContent, TextContent, ToolContext, ToolResult, text_result

if TYPE_CHECKING:
    from src.tools.tool_manager import ToolManager

# 当前模块使用的日志记录器。
logger = logging.getLogger(__name__)


def resolve_path(path: str, context: ToolContext) -> Path:
    """以本轮工作目录为基准解析路径。"""
    # 这里的 / 是 pathlib.Path 重载的路径拼接运算符，不是简单的字符串拼接。
    # 当右侧是完整的绝对路径时，会直接采用右侧路径，舍弃左侧的工作区。
    return (context.cwd / path).resolve()


def run_get_model_name(*, model_name: str) -> ToolResult:
    """读取与当前模型请求相同的统一配置，返回配置的模型名称。"""
    return text_result(model_name)


def run_get_current_time() -> ToolResult:
    """返回系统本地当前时间，使用带时区偏移的 ISO 8601 格式，精确到秒。"""
    return text_result(datetime.now().astimezone().isoformat(timespec="seconds"))


def run_powershell(command: str, workdir: str | None = None, *, timeout: int,
                   allowed_dirs: tuple[Path, ...], context: ToolContext) -> ToolResult:
    """在 MXC 沙箱内执行 PowerShell，允许读写配置的目录、用户审批通过的workdir和本次工作目录cwd。"""
    try:
        cwd = resolve_path(workdir if workdir is not None else ".", context)
        roots = dict.fromkeys((*allowed_dirs, context.cwd.resolve(), cwd))
        # TOML 内联表的路径键单独加引号，整个策略作为一个 CLI 参数传递。
        rules = ['":minimal"="read"']  # 为 PowerShell 和子进程开放基础运行环境的读取权限。
        rules.extend(f'{json.dumps(path.as_posix(), ensure_ascii=False)}="write"' for path in roots)
        codex = shutil.which("codex.exe")
        if codex is None:
            raise FileNotFoundError("PATH 中找不到 codex.exe")
        with subprocess.Popen(
            [
                codex, "-c", "windows.sandbox=mxc", "sandbox",
                "-c", "permissions.miniagent.filesystem={" + ", ".join(rules) + "}",
                "-P", "miniagent", "--include-managed-config", "--",
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + command,
            ],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        ) as process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # Windows 上单独结束 Codex 会留下正在执行命令的子进程。
                taskkill = Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
                killed = subprocess.run([taskkill, "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                if killed.returncode and process.poll() is None:
                    logger.error("终止 MXC 进程树失败，PID=%s，退出码=%s", process.pid, killed.returncode)
                    process.kill()
                    return text_result(f"PowerShell 执行超过 {timeout} 秒，无法确认子进程已终止。", is_error=True)
                stdout, stderr = process.communicate()
                logger.warning("MXC PowerShell 执行超时，限时 %s 秒", timeout)
                output = {"error": f"PowerShell 执行超过 {timeout} 秒，已终止。", "stdout": stdout, "stderr": stderr}
                return text_result(json.dumps(output, ensure_ascii=False), is_error=True)
        output = {"exit_code": process.returncode, "stdout": stdout, "stderr": stderr}
        # PowerShell 非终止错误可能留下退出码 0，仍需向模型报告明确的权限错误。
        diagnostics = "\n".join(line for line in stderr.casefold().splitlines() if not line.startswith("warning:"))
        denied = any(marker in diagnostics for marker in
                     ("unauthorizedaccess", "permissiondenied", "access is denied", "permission denied", "拒绝访问"))
        if denied:
            output["error"] = "沙箱不允许该操作，权限不足。请向用户说明操作未能完成；若 stderr 指明了被拒绝的路径，请一并告知。"
            logger.warning("PowerShell 操作被拒绝，工作目录：%s", cwd)
        return text_result(json.dumps(output, ensure_ascii=False), is_error=denied or process.returncode != 0)
    except ValueError as error:
        return text_result(str(error), is_error=True)
    except OSError as error:
        logger.error("无法启动 MXC PowerShell：%s", error)
        return text_result(f"无法启动 MXC PowerShell：{error}", is_error=True)


def run_read(path: str, limit: int | None = None, *, context: ToolContext) -> ToolResult:
    """读取指定的 UTF-8 文本文件，可选择限制返回的行数。"""
    try:
        lines = resolve_path(path, context).read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return text_result("\n".join(lines))
    except Exception as error:
        return text_result(str(error), is_error=True)


def image_mime_type(data: bytes) -> str:
    """根据文件头识别支持的图片格式。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("无法识别图片格式，仅支持 PNG、JPEG、GIF 和 WebP")


def run_read_images(paths: list[str], *, context: ToolContext) -> ToolResult:
    """按传入顺序返回路径和图片，单张读取失败不影响其他图片。"""
    if not paths:
        logger.warning("批量读取图片时未提供路径")
        return text_result("图片路径列表不能为空", is_error=True)
    blocks: list[TextContent | ImageContent] = []
    errors: list[str] = []
    for path in paths:
        try:
            data = resolve_path(path, context).read_bytes()
            mime_type = image_mime_type(data)
        except (OSError, ValueError) as error:
            message = f"读取图片失败：{path}：{error}"
            logger.warning(message)
            errors.append(message)
            blocks.append(TextContent(message))
            continue
        blocks.append(TextContent(f"图片：{path}"))
        blocks.append(ImageContent(mime_type, base64.b64encode(data).decode("ascii")))
    if len(errors) == len(paths):
        return text_result("\n".join(errors), is_error=True)
    return ToolResult(blocks)


def run_write(path: str, content: str, *, context: ToolContext) -> ToolResult:
    """向指定的 UTF-8 文本文件写入内容，并按需创建父目录。"""
    try:
        file_path = resolve_path(path, context)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return text_result(f"已向 {file_path} 写入 {len(content)} 个字符")
    except Exception as error:
        return text_result(str(error), is_error=True)


def run_edit(path: str, old_text: str, new_text: str, *, context: ToolContext) -> ToolResult:
    """在指定的 UTF-8 文本文件中替换第一次出现的指定文本。"""
    try:
        file_path = resolve_path(path, context)
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text:
            return text_result(f"在 {file_path} 中找不到待替换文本", is_error=True)
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return text_result(f"已修改 {file_path}")
    except Exception as error:
        return text_result(str(error), is_error=True)


def run_glob(pattern: str, workdir: str | None = None, *, context: ToolContext) -> ToolResult:
    """从指定目录或默认工作区查找 glob 匹配项，最多返回 200 项。"""
    import glob as glob_module

    try:
        cwd = resolve_path(workdir if workdir is not None else ".", context)
        if not cwd.is_dir():
            logger.warning("glob 工作目录不存在或不是目录：%s", cwd)
            return text_result(f"错误：工作目录不存在或不是目录：{cwd}", is_error=True)
        matches: set[str] = set()
        for match in glob_module.glob(pattern, root_dir=cwd, recursive=True):
            # 返回绝对路径，供读写工具直接使用。
            matches.add(str((cwd / match).resolve()))
        shown = sorted(matches)[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return text_result("\n".join(shown) if shown else "没有匹配项")
    except Exception as error:
        return text_result(str(error), is_error=True)


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
            return CREATE_PLAN_REMINDER.format(interval=self.REMINDER_INTERVAL)
        return UPDATE_PLAN_REMINDER.format(interval=self.REMINDER_INTERVAL, plan=self.render())

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
async def run_update_plan(plan: list[Any] | str, *, context: ToolContext) -> ToolResult:
    """在事件循环内更新当前 turn 的计划，展示由工具结果事件交给通道完成。"""
    try:
        output = context.plan.update(plan)
    except ValueError as error:
        logger.warning("当前 turn 的计划更新失败：%s", error)
        return text_result(str(error), is_error=True)
    logger.info("当前 turn 计划已更新")
    return text_result(output)


def run_load_skill(name: str) -> ToolResult:
    """读取技能完整说明，调用时扫描目录以取得当前可用技能。"""
    return SkillLoader(PROJECT_ROOT / SKILLS_DIR_NAME).load(name)


def register_local_tools(registry: "ToolManager", config: Config) -> None:
    """集中注册本地工具，并绑定各工具需要的运行时配置。"""
    # 提供给模型的工具定义；parameters 使用 JSON Schema 描述模型可传入的参数。
    registry.register_local_tool(
        name="powershell",
        func=partial(run_powershell, timeout=config.powershell_timeout, allowed_dirs=config.allowed_dirs),
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
        supports_parallel=True,
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
        name="read_images",
        supports_parallel=True,
        func=run_read_images,
        description="按顺序读取本地图片（PNG、JPEG、GIF、WebP）。支持绝对路径和相对于当前工作目录的路径。",
        parameters={
            "type": "object",
            "properties": {"paths": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
            "required": ["paths"],
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
        supports_parallel=True,
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
        supports_parallel=True,
        func=run_load_skill,
        description="按名称读取某项技能的完整说明。仅在技能适用于当前任务时调用。",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "技能目录中列出的技能名称。"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        strict=True,
    )

    if config.long_term_memory_enabled:
        registry.register_local_tool(
            name="load_memory",
            supports_parallel=True,
            func=load_memory,
            description="根据长期记忆目录中的文件名，按需读取一条长期记忆的完整内容。",
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
        supports_parallel=True,
        func=partial(run_get_model_name, model_name=config.model),
        description="获取当前请求配置的模型名称，读取统一运行配置。",
        parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        strict=True,
    )

    registry.register_local_tool(
        name="get_current_time",
        supports_parallel=True,
        func=run_get_current_time,
        description="获取系统本地当前时间，以带时区偏移的 ISO 8601 格式返回，精确到秒，例如 2026-11-11T15:30:45+08:00。",
        parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        strict=True,
    )
