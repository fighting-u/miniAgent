import asyncio
import logging
from contextlib import aclosing
from copy import deepcopy
from typing import Any

from openai import AsyncOpenAI

from src.agent.core import agent_loop
from src.agent.hook import trigger_hooks
from src.channels.base import ChannelDisconnected, InboundType, MessageDispatcher, InboundMessage
from src.channels.manager import ChannelManager
from src.config import Config, PROJECT_ROOT
from src.memory.memory import extract_memories
from src.runtime.permissions import ApprovalBroker
from src.session.session import Session, get_owner_id
from src.session.session_manager import SessionManager, serializable
from src.session.storage import SESSION_DIR_NAME
from src.tools.tool_manager import ToolManager


# 会话协调、请求执行和持久化的日志。
logger = logging.getLogger(__name__)


class AgentRuntime:
    """运行时"""

    def __init__(self, config: Config) -> None:
        """保存统一配置，管理 Agent 资源、通道、会话与事件分发的完整生命周期。"""
        self.config = config
        if not config.api_key or not config.model.strip():
            raise ValueError(f"请在 config.yaml 配置 model，并设置 {config.api_key_env} 环境变量")
        self.client = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url)  # 构造客户端，stop 时关闭。
        self.tool_manager = ToolManager(config)  # 共享工具管理器，start 时注册工具并建立 MCP 连接。
        self.max_concurrent_requests = asyncio.Semaphore(config.max_concurrent_requests)  # 请求并发上限。
        self.requests: dict[asyncio.Task, InboundMessage | ChannelDisconnected] = {}  # 跟踪已接受的输入任务，退出时等待完成。
        self.message_dispatcher = MessageDispatcher()
        # todo: ?
        self.approvals = ApprovalBroker(self.message_dispatcher)
        self.session_manager = SessionManager(PROJECT_ROOT / SESSION_DIR_NAME, config)  # 管理会话历史、存储和入口绑定。
        self.channels = ChannelManager(config, self.message_dispatcher, self.submit, self.session_manager)  # 共享配置和消息分发器，并传入提交回调。
        # 保留本进程创建的后台记忆任务，退出前显式等待并收集异常。
        self.background_tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """会话按入口选择懒加载，先准备工具再启动通道。"""
        # 通道收到输入后立即提交，先准备工具，保证请求执行时工具已就绪。
        await self.tool_manager.start()
        await self.channels.start()
        logger.info("AgentRuntime 已启动：model=%s workdir=%s", self.config.model, self.config.workdir)

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
        await self.message_dispatcher.send(message, {
            "type": "session", "title": title, "items": items,
            "model": self.config.model, "workdir": str(self.config.workdir),
        })
        logger.info("入口已选择会话：channel=%s session=%s", message.channel_id, session_id)

    async def _history(self, message: InboundMessage) -> None:
        """返回只读历史；客户端启动未指定会话时查询该入口保存的选择。"""
        message.session_id = message.session_id or self.session_manager.get_binding(message.channel_id)
        items = await self.session_manager.history(message.session_id)
        await self.message_dispatcher.send(message, {
            "type": "session_history", "title": "会话历史" if message.session_id else "新会话",
            "items": items, "model": self.config.model, "workdir": str(self.config.workdir),
        })

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
                task is not asyncio.current_task() and isinstance(request, InboundMessage)
                and request.session_id == session_id
                for task, request in self.requests.items()
            ):
                raise ValueError("该会话仍有等待或执行中的请求，请稍后删除。")
            self.session_manager.delete(session_id)
            if session_id == message.session_id:
                await self._select_session(message, "", "当前会话已删除，已开启新会话")
            await self.message_dispatcher.send(message, {"type": "status", "text": "已删除所选会话。"})
        else:
            raise ValueError("未知命令，可用命令：/new、/resume、/delete、/q")

    async def process_event(self, message: InboundMessage, session: Session, event: dict[str, Any]) -> None:
        """原始事件先落盘再分发，审批事件在原对象中填写决定。"""
        if event["type"] == "items":
            for item in event["items"]:
                self.session_manager.append(session, item, model_name=event.get("model_name"))
            await self.message_dispatcher.send(message, {"type": "items", "items": [serializable(item) for item in event["items"]]})
        elif event["type"] == "permission":
            event["allowed"] = await self.approvals.request(message, event)
        else:
            await self.message_dispatcher.send(message, event)

    async def _run_turn(self, message: InboundMessage) -> None:
        """用户输入已落盘，由调用方取得会话占用后执行 Agent 并消费事件。"""
        session = self.session_manager.get(message.session_id)
        # todo：传那么多参数，是否会太乱以及不易维护？配置集中传入，资源由本运行时管理。
        try:
            async with aclosing(agent_loop(self.client, session.messages, message.input,
                                          self.config, self.tool_manager)) as events:
                async for event in events:
                    await self.process_event(message, session, event)
        except BaseException:
            # 生成器中断后以内存外的原始记录重建历史，下轮再补齐缺失工具结果。
            self.session_manager.reload(session)
            raise
        # Agent 正常结束后，提取本轮快照，不受后续追加和压缩影响。
        self.background_tasks.append(asyncio.create_task(
            extract_memories(self.client, deepcopy(session.messages), self.config)))

    def submit(self, message: InboundMessage | ChannelDisconnected) -> None:
        """只创建和登记输入任务，提交后立即返回，不等待业务处理。"""
        task = asyncio.create_task(self._process_message(message), name=f"message-{message.channel_id}")
        self.requests[task] = message
        task.add_done_callback(self.requests.pop)

    async def _process_message(self, message: InboundMessage | ChannelDisconnected) -> None:
        """按输入类型分发；断线、审批答复和会话占用判断先于并发名额等待。"""
        if isinstance(message, ChannelDisconnected):
            self.approvals.disconnect(message.channel_id, message.reply_target)
            return
        if message.type == InboundType.COMMAND and message.input.startswith("/approve "):
            self.approvals.resolve(message)
            return
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
                    logger.warning("会话占用冲突：session_id =%s 请求人=%s 会话当前的owner=%s",
                                   session.id, requester_id, session.owner_id)
                    await self.message_dispatcher.send(message, {
                        "type": "error", "text": f"该会话已被 {session.owner_id} 占用，请稍后再试。",
                    })
                    return
                if not message.session_id:
                    message.session_id = session.id
                    # 首次创建只回传 ID，入口保存标识，不刷新界面或回显用户消息。
                    await self.message_dispatcher.send(message, {"type": "session_created"})
                async with self.max_concurrent_requests:
                    trigger_hooks("PreUserPromptSubmit", self.config)
                    self.session_manager.start_turn(session, message.input)
                    self.session_manager.bind(message.channel_id, session.id)
                    await self._run_turn(message)
                    self.session_manager.release_session_lock(session)
        except Exception as error:
            logger.exception("请求处理失败：request=%s session=%s", message.request_id, message.session_id)
            await self.message_dispatcher.send(message, {"type": "error", "text": str(error)})
        finally:
            await self.message_dispatcher.send(message, {"type": "done"})

    async def stop(self) -> None:
        """停止通道、完成请求和后台任务后关闭工具和模型客户端，允许启动失败后调用。"""
        try:
            try:
                await self.channels.stop()
            finally:
                await self._drain()
        finally:
            try:
                await self.tool_manager.stop()
            finally:
                await self.client.close()
        logger.info("AgentRuntime 已停止，共享模型与 MCP 资源已关闭")

    async def _drain(self) -> None:
        """等待已接受请求和后台记忆任务，保持客户端可用到最后。"""
        while self.requests:
            await asyncio.gather(*list(self.requests), return_exceptions=True)
        if self.background_tasks:
            results = await asyncio.gather(*self.background_tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    logger.warning("后台记忆提取任务未正常完成：%s", result)
            self.background_tasks.clear()
