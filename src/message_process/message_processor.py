import asyncio
import logging
from pathlib import Path

from openai import AsyncOpenAI

from src.channels.base import InboundMessage, InboundType, MessageDispatcher
from src.config import Config
from src.hook.hook import trigger_hooks
from src.message_process.turn_runner import TurnRunner
from src.session.session import get_owner_id
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
        message_dispatcher: MessageDispatcher,
        session_manager: SessionManager,
        requests: dict[asyncio.Task, InboundMessage],
        background_tasks: list[asyncio.Task],
    ) -> None:
        """保存共享依赖，并创建并发信号量和轮次执行器。"""
        self.config = config
        self.message_dispatcher = message_dispatcher
        self.session_manager = session_manager
        self.max_concurrent_requests = asyncio.Semaphore(config.max_concurrent_requests)  # 请求并发上限。
        self.requests = requests
        self.turn_runner = TurnRunner(
            client=client,
            config=config,
            tool_manager=tool_manager,
            session_manager=session_manager,
            message_dispatcher=message_dispatcher,
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
        await self.message_dispatcher.send(
            message, {"type": "session", "title": title, "items": items, "model": self.config.model, "workdir": str(Path.cwd().resolve())}
        )
        logger.info("入口已选择会话：channel=%s session=%s", message.channel_id, session_id)

    async def _history(self, message: InboundMessage) -> None:
        """返回只读历史；客户端启动未指定会话时查询该入口保存的选择。"""
        message.session_id = message.session_id or self.session_manager.get_binding(message.channel_id)
        items = await self.session_manager.history(message.session_id)
        await self.message_dispatcher.send(
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
        await self.message_dispatcher.send(message, {"type": "sessions", "sessions": self.session_manager.list_sessions()})

    async def _command(self, message: InboundMessage) -> None:
        """识别斜杠命令；菜单在客户端展示，文件操作由常驻端执行。"""
        command, _, argument = message.input.partition(" ")
        argument = argument.strip()
        if command == "/new":
            await self._select_session(message, "", "新会话")
        elif command in {"/resume", "/delete"} and not argument:
            await self._list_sessions(message)
        elif command == "/resume":
            await self._select_session(message, argument, "已恢复会话")
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
            await self.message_dispatcher.send(message, {"type": "status", "text": "已删除所选会话。"})
        else:
            raise ValueError("未知命令，可用命令：/new、/resume、/delete、/q")

    async def process_message(self, message: InboundMessage) -> None:
        """按输入类型分发，会话占用判断先于并发名额等待。"""
        try:
            # todo: 优化太多If,else
            if message.type == InboundType.GET_HISTORY:
                await self._history(message)
            elif message.type == InboundType.LIST_SESSIONS:
                await self._list_sessions(message)
            elif message.type == InboundType.COMMAND:
                async with self.max_concurrent_requests:
                    await self._command(message)
            elif message.input:
                # 接受时固定目标会话，后续其他入口切换会话不改变本次请求归属。
                # 按内存、磁盘、新建的顺序取得会话，/new 的标识到这里才创建对象。
                session = self.session_manager.get_or_create(message.session_id)
                requester_id = get_owner_id(message.channel_id, message.reply_target)
                if not self.session_manager.try_acquire_session_lock(session, requester_id):
                    logger.warning("会话占用冲突：session_id =%s 请求人=%s 会话当前的owner=%s", session.id, requester_id, session.owner_id)
                    await self.message_dispatcher.send(message, {"type": "error", "text": f"该会话已被 {session.owner_id} 占用，请稍后再试。"})
                    return
                if not message.session_id:
                    message.session_id = session.id
                    # 首次创建只回传 ID，入口保存标识，不刷新界面或回显用户消息。
                    await self.message_dispatcher.send(message, {"type": "session_created"})
                async with self.max_concurrent_requests:
                    trigger_hooks("PreUserPromptSubmit", Path.cwd().resolve())
                    self.session_manager.start_turn(session, message.input)
                    self.session_manager.bind(message.channel_id, session.id)
                    await self.turn_runner.run_turn(message)
                    self.session_manager.release_session_lock(session)
        except Exception as error:
            logger.exception("请求处理失败：request=%s session=%s", message.request_id, message.session_id)
            await self.message_dispatcher.send(message, {"type": "error", "text": str(error)})
        finally:
            await self.message_dispatcher.send(message, {"type": "done"})
