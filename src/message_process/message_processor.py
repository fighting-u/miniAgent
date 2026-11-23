import asyncio
import logging
from pathlib import Path

from openai import AsyncOpenAI

from src.channels.base import InboundMessage, InboundType
from src.compact.compactor import ContextCompactor
from src.config import Config
from src.hook.hook import HookManager
from src.message_process.turn_runner import TurnRunner
from src.session.session import Session, get_owner_id
from src.session.session_manager import SessionManager
from src.tools.tool_manager import ToolManager

# 记录请求处理和会话选择情况。
logger = logging.getLogger(__name__)


class MessageProcessor:
    """处理通道提交的消息，并将聊天轮次交给 TurnRunner。"""

    def __init__(
        self,
        config: Config,
        client: AsyncOpenAI,
        tool_manager: ToolManager,
        session_manager: SessionManager,
        compactor: ContextCompactor,
        hook_manager: HookManager,
        requests: dict[asyncio.Task, InboundMessage],
        background_tasks: list[asyncio.Task],
    ) -> None:
        """保存共享依赖，并创建并发信号量和轮次执行器。"""
        self.config = config
        self.session_manager = session_manager
        self.compactor = compactor  # 全局共享上下文压缩器，供手动 /compact 和自动检测共用。
        self.hook_manager = hook_manager  # 触发用户输入前的 Hook。
        self.max_concurrent_requests = asyncio.Semaphore(config.max_concurrent_requests)  # 请求并发上限。
        self.requests = requests
        self.session_choices: dict[str, list[str]] = {}  # 各入口最近展示的编号顺序，不随会话更新时间改变。
        self.turn_runner = TurnRunner(
            client=client,
            config=config,
            tool_manager=tool_manager,
            session_manager=session_manager,
            compactor=compactor,
            hook_manager=hook_manager,
            background_tasks=background_tasks,
        )

    async def _select_session(self, message: InboundMessage, session_id: str, title: str) -> None:
        """已有 ID 严格恢复；空 ID 只分配标识，首次聊天才创建 Session。"""
        if session_id:
            session = self.session_manager.get(session_id)
            items = await self.session_manager.history(session_id)
            self.session_manager.bind(message.channel_id, session_id if session.persisted else "")
        else:
            session_id = self.session_manager.new_session_id()
            items = []
            # 尚未实际聊天的新标识不绑定到磁盘会话，重启后仍为空白。
            self.session_manager.bind(message.channel_id, "")
        message.session_id = session_id
        await self.session_manager.message_dispatcher.send(
            message, {"type": "session", "title": title, "items": items, "model": self.config.model, "workdir": str(Path.cwd().resolve())}
        )
        logger.info("入口已选择会话：channel=%s session=%s", message.channel_id, session_id)

    async def _history(self, message: InboundMessage) -> None:
        """返回只读历史；客户端启动未指定会话时查询该入口保存的选择。"""
        message.session_id = message.session_id or self.session_manager.get_binding(message.channel_id)
        items = await self.session_manager.history(message.session_id)
        await self.session_manager.message_dispatcher.send(
            message,
            {
                "type": "session_history",
                "title": "会话历史" if message.session_id else "新会话",
                "items": items,
                "model": self.config.model,
                "workdir": str(Path.cwd().resolve()),
            },
        )

    async def _list_sessions(self, message: InboundMessage) -> None:
        """发送会话列表，不创建会话或改变入口选择。"""
        entries = self.session_manager.list_sessions()
        if await self.session_manager.message_dispatcher.send(message, {"type": "sessions", "sessions": entries}):
            owner_id = get_owner_id(message.channel_id, message.reply_target)
            self.session_choices[owner_id] = [entry["id"] for entry in entries]

    async def _acquire_session(self, message: InboundMessage, session: Session, requester_id: str) -> bool:
        """尝试取得会话占用；冲突时按同来源忙碌或其他来源占用发送中文提示。"""
        if self.session_manager.try_acquire_session_lock(session, requester_id):
            return True
        if session.owner_id == requester_id:
            logger.warning("会话正在处理请求：session_id=%s requester_id=%s", session.id, requester_id)
            await self.session_manager.message_dispatcher.send(message, {"type": "error", "text": "当前会话正在处理请求，请稍后再试。"})
        else:
            logger.warning("会话占用冲突：session_id=%s 请求人=%s 会话当前的owner=%s", session.id, requester_id, session.owner_id)
            await self.session_manager.message_dispatcher.send(message, {"type": "error", "text": f"该会话已被 {session.owner_id} 占用，请稍后再试。"})
        return False

    async def _compact(self, message: InboundMessage, argument: str) -> None:
        """手动压缩当前会话的完整有效历史，不递增 turn_id 也不创建空会话文件。"""
        if argument:
            raise ValueError("/compact 暂不接受额外参数。")
        session = self.session_manager.get(message.session_id) if message.session_id else None
        if session is None or not session.persisted or not session.messages:
            await self.session_manager.message_dispatcher.send_status(message, "当前会话还没有可压缩的历史。")
            return
        requester_id = get_owner_id(message.channel_id, message.reply_target)
        if not await self._acquire_session(message, session, requester_id):
            return
        try:
            async with self.max_concurrent_requests:
                await self.session_manager.message_dispatcher.send_status(message, "正在压缩上下文...")
                try:
                    self.session_manager.recover_pending_tools(session)
                    await self.compactor.compact(session, phase="manual")
                except BaseException:
                    self.session_manager.reload(session)
                    raise
                await self.session_manager.message_dispatcher.send_status(message, "上下文已压缩。")
        finally:
            self.session_manager.release_session_lock(session)

    async def _command(self, message: InboundMessage) -> None:
        """统一识别并处理所有斜杠命令；菜单在客户端展示，文件与压缩操作由常驻端执行。"""
        command, _, argument = message.input.partition(" ")
        argument = argument.strip()
        if command == "/new":
            await self._select_session(message, "", "新会话")
        elif command == "/compact":
            await self._compact(message, argument)
        elif command in {"/resume", "/delete"} and not argument:
            await self._list_sessions(message)
        elif command == "/resume":
            number = int(argument)
            choices = self.session_choices.get(get_owner_id(message.channel_id, message.reply_target), [])
            if not choices:
                raise ValueError("请先发送 /resume 查看历史会话列表。")
            if not 1 <= number <= len(choices):
                raise ValueError(f"会话编号超出范围，请输入 1 到 {len(choices)} 之间的编号。")
            await self._select_session(message, choices[number - 1], "已恢复会话")
        elif command == "/delete":
            session_id, _, confirmation = argument.partition(" ")
            if confirmation != "confirm":
                raise ValueError("删除前需要客户端二次确认。")
            if any(
                task is not asyncio.current_task() and request.session_id == session_id
                for task, request in self.requests.items()
            ):
                raise ValueError("该会话仍有等待或执行中的请求，请稍后删除。")
            self.session_manager.delete(session_id)
            if session_id == message.session_id:
                await self._select_session(message, "", "当前会话已删除，已开启新会话")
            await self.session_manager.message_dispatcher.send_status(message, "已删除所选会话。")
        else:
            raise ValueError("未知命令，可用命令：/new、/resume、/compact、/delete、/q")

    async def process_message(self, inbound_message: InboundMessage) -> None:
        """按输入类型分发，普通聊天优先匹配当前会话的待审批请求。"""
        # todo: 优化done事件，event类型
        context_window_usage = None
        try:
            inbound_message.input = inbound_message.input.strip()
            # todo: 优化太多If,else
            if inbound_message.type == InboundType.GET_HISTORY:
                await self._history(inbound_message)
            elif inbound_message.type == InboundType.LIST_SESSIONS:
                await self._list_sessions(inbound_message)
            elif inbound_message.type == InboundType.COMMAND:
                await self._command(inbound_message)
            elif inbound_message.input:
                # 用户审批处理
                requester_id = get_owner_id(inbound_message.channel_id, inbound_message.reply_target)
                session = self.session_manager.sessions.get(inbound_message.session_id)
                if session is not None and session.owner_id == requester_id and session.pending_approval is not None:
                    approval = session.pending_approval
                    if inbound_message.input in {"yes", "no"} and not approval.done():
                        approval.set_result(inbound_message.input == "yes")
                        logger.info("工具审批答复：session=%s answer=%s", session.id, inbound_message.input)
                    # 审批期间的输入到此为止，finally 仍会发送 done 以清理通道回复记录。
                    return

                # 接受时固定目标会话，后续其他入口切换会话不改变本次请求归属。
                # 按内存、磁盘、新建的顺序取得会话，/new 的标识到这里才创建对象。
                if session is None:
                    session = self.session_manager.get_or_create(inbound_message.session_id)
                if not await self._acquire_session(inbound_message, session, requester_id):
                    # 获取会话锁失败
                    return
                try:
                    if not inbound_message.session_id:
                        inbound_message.session_id = session.id
                        # 首次创建只回传 ID，入口保存标识，不刷新界面或回显用户消息。
                        await self.session_manager.message_dispatcher.send(inbound_message, {"type": "session_created"})
                    async with self.max_concurrent_requests:
                        self.session_manager.bind(inbound_message.channel_id, session.id)
                        self.hook_manager.trigger("PreUserPromptSubmit", Path.cwd().resolve())
                        await self.turn_runner.run_turn(inbound_message)
                        context_window_usage = self.compactor.format_window_usage(session)
                finally:
                    self.session_manager.release_session_lock(session)
        except Exception as error:
            logger.exception("请求处理失败：request=%s session=%s", inbound_message.request_id, inbound_message.session_id)
            await self.session_manager.message_dispatcher.send(inbound_message, {"type": "error", "text": str(error)})
        finally:
            await self.session_manager.message_dispatcher.send_done(inbound_message, context_window_usage)
