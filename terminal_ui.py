import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import has_completions
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import clear
from prompt_toolkit.styles import Style


# 输入框支持的斜杠命令及中文补全说明。
COMMANDS = {"/new": "开启新对话", "/resume": "选择历史会话", "/delete": "选择并删除会话", "/q": "退出程序"}

# 终端输入、菜单和角色标题的轻量配色。
UI_STYLE = Style.from_dict({
    "prompt": "#5f87ff bold",
    "placeholder": "#888888",
    "bottom-toolbar": "bg:#303030 #dddddd",
    "completion-menu.completion": "bg:#303030 #dddddd",
    "completion-menu.completion.current": "bg:#5f87ff #ffffff",
    "completion-menu.meta.completion": "bg:#303030 #aaaaaa",
    "completion-menu.meta.completion.current": "bg:#5f87ff #ffffff",
    "user": "#5f87ff bold",
    "assistant": "#5faf87 bold",
    "system": "#af87ff bold",
    "detail": "#888888",
})

# 工具参数、推理和工具结果的展示长度，不影响实际保存或发送的内容。
DETAIL_PREVIEW_CHARS = 600


def item_value(item: Any, name: str, default: Any = None) -> Any:
    """统一读取加载后的字典和当前 API 返回的 SDK 对象。"""
    return item.get(name, default) if isinstance(item, dict) else getattr(item, name, default)


def content_text(content: Any) -> str:
    """提取消息正文或推理摘要中的文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item_value(block, "text", "")) for block in content
                         if item_value(block, "text"))
    return ""


class CommandCompleter(Completer):
    """仅在输入以斜杠开头时显示命令菜单。"""

    def get_completions(self, document: Document, complete_event: Any) -> Iterable[Completion]:
        """按当前输入前缀补全命令，并附带中文用途说明。"""
        prefix = document.text_before_cursor
        if not prefix.startswith("/") or any(char.isspace() for char in prefix):
            return
        for command, description in COMMANDS.items():
            if command.startswith(prefix):
                yield Completion(command, start_position=-len(prefix), display_meta=description)


class SessionCompleter(Completer):
    """在选择菜单里展示时间和首条问题，实际选择值使用临时序号。"""

    def __init__(self, sessions: list[dict[str, Any]]) -> None:
        """保存此次打开菜单时的会话快照，避免选择过程中顺序变化。"""
        self.sessions = sessions

    def get_completions(self, document: Document, complete_event: Any) -> Iterable[Completion]:
        """生成可用上下键选择或输入序号筛选的历史会话选项。"""
        prefix = document.text_before_cursor
        for index, session in enumerate(self.sessions, start=1):
            number = str(index)
            if number.startswith(prefix):
                timestamp = datetime.fromtimestamp(session["updated_at"]).strftime("%Y-%m-%d %H:%M")
                display = f"{index:>2}  {timestamp}  {session['preview']}"
                yield Completion(number, start_position=-len(prefix), display=display)


def menu_bindings(ignore_blank: bool = False) -> KeyBindings:
    """让 Enter 接受菜单选项，主输入框可拦截空提交，其他按键沿用默认行为。"""
    bindings = KeyBindings()

    @bindings.add("enter", filter=has_completions)
    def accept_completion(event: Any) -> None:
        """没有选中项时接受第一项，选中后立即提交当前输入。"""
        buffer = event.current_buffer
        if buffer.complete_state.current_completion is None:
            buffer.go_to_completion(0)
        buffer.complete_state = None
        buffer.validate_and_handle()

    @bindings.add("enter", filter=~has_completions)
    def accept_input(event: Any) -> None:
        """空问题留在当前输入框，避免退出 prompt 后重新打印占位行。"""
        buffer = event.current_buffer
        if ignore_blank and not buffer.text.strip():
            buffer.text = ""
            return
        buffer.validate_and_handle()

    return bindings


class TerminalUI:
    """负责终端输入、会话选择以及历史内容展示，不参与模型或存储逻辑。"""

    def __init__(self, model_name: str) -> None:
        """配置单行输入框、占位文字、斜杠菜单和底部提示。"""
        self.prompt = PromptSession(
            message=[("class:prompt", "› ")],
            placeholder=[("class:placeholder", "输入问题，或输入 / 查看命令")],
            completer=CommandCompleter(), complete_while_typing=True,
            key_bindings=menu_bindings(ignore_blank=True), style=UI_STYLE, reserve_space_for_menu=len(COMMANDS),
            bottom_toolbar=f"{model_name} · {Path.cwd()}\n/new 新对话   /resume 历史会话   /delete 删除会话   /q 退出",
        )

    def read_input(self) -> str:
        """读取一次有效用户输入，空白回车保留在同一个输入框。"""
        return self.prompt.prompt().strip()

    def choose_session(self, sessions: list[dict[str, Any]], action: str = "恢复") -> Path | None:
        """复用同一个会话选择列表，供恢复或删除使用，取消时不改变当前会话。"""
        if not sessions:
            print("暂无可选择的会话。")
            return None
        selector = PromptSession(
            message=f"{action} › ", completer=SessionCompleter(sessions), complete_while_typing=True,
            key_bindings=menu_bindings(), style=UI_STYLE, reserve_space_for_menu=10,
            placeholder="选择会话或输入序号；Ctrl+C 取消",
            bottom_toolbar="最近时间 · 首条用户问题    ↑↓ 选择  Tab 补全  Enter 确认",
        )

        def show_choices() -> None:
            """进入选择框时立即展开全部会话。"""
            selector.default_buffer.start_completion(select_first=False)

        while True:
            try:
                choice = selector.prompt(pre_run=show_choices).strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if not choice:
                return None
            if choice.isdecimal() and 1 <= int(choice) <= len(sessions):
                return sessions[int(choice) - 1]["path"]
            print("请输入列表内的有效序号，或按 Ctrl+C 取消。")

    def confirm_delete(self, session_info: dict[str, Any], is_current: bool) -> bool:
        """显示选中会话的时间和问题，只有明确输入 y 或 yes 才允许删除。"""
        timestamp = datetime.fromtimestamp(session_info["updated_at"]).strftime("%Y-%m-%d %H:%M")
        print(f"准备删除：{timestamp}  {session_info['preview']}")
        print("删除后无法通过程序恢复。")
        if is_current:
            print("这是当前会话，删除后将清屏并开启新会话。")
        confirmation = PromptSession(style=UI_STYLE)
        try:
            answer = confirmation.prompt("确认删除？[y/N] ").strip().casefold()
        except (EOFError, KeyboardInterrupt):
            return False
        return answer in {"y", "yes"}

    def show_session(self, history: list[Any], title: str) -> None:
        """清理当前屏幕并展示选中会话的历史，不修改或重放记录。"""
        # 先提交已有输出，再使用 Windows 原生清屏，避免旧菜单留在控制台缓冲区。
        sys.stdout.flush()
        if os.name != "nt" or os.system("cls") != 0:
            clear()
        print_formatted_text(FormattedText([("class:assistant", f"MiniAgent · {title}")]), style=UI_STYLE)
        print()
        if history:
            self.show_items(history)
        else:
            print("还没有聊天记录，输入问题开始对话。\n")

    @staticmethod
    def show_items(items: list[Any]) -> None:
        """统一渲染历史和实时事件，不依赖输入框实例，细节内容提供有限预览。"""
        for item in items:
            kind = item_value(item, "type", "message")
            style = "detail"
            if kind == "message":
                role = item_value(item, "role", "assistant")
                label = {"user": "你", "assistant": "助手", "system": "系统", "developer": "开发者"}.get(role, role)
                style = role if role in {"user", "assistant", "system"} else "detail"
                text = content_text(item_value(item, "content"))
            elif kind == "reasoning":
                label = "推理摘要"
                text = content_text(item_value(item, "summary"))
            elif kind == "function_call":
                label = f"工具调用 · {item_value(item, 'name', '')}"
                text = str(item_value(item, "arguments", ""))
            elif kind == "function_call_output":
                label = "工具结果"
                text = str(item_value(item, "output", ""))
            else:
                continue
            if kind != "message" and len(text) > DETAIL_PREVIEW_CHARS:
                text = text[:DETAIL_PREVIEW_CHARS] + "\n…（展示已截断，完整内容保留在会话文件中）"
            if text:
                print_formatted_text(FormattedText([(f"class:{style}", f"{label}\n")]), style=UI_STYLE)
                print_formatted_text(text)
                print()
