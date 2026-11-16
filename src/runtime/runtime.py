import asyncio
import logging
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI

from src.agent.core import agent_loop
from src.agent.hook import trigger_hooks
from src.channels.base import ChannelDisconnected, MessageDispatcher, InboundMessage
from src.channels.manager import ChannelManager
from src.config import Config, PROJECT_ROOT
from src.memory.memory import extract_memories
from src.runtime.permissions import ApprovalBroker
from src.session.session_store import SESSION_DIR_NAME, SessionStore
from src.tools.mcp_manager import MCPManager
from src.tools.tools import PlanManager


# 会话协调、请求执行和持久化的日志。
logger = logging.getLogger(__name__)


def serializable(item: Any) -> Any:
    """转换 SDK 消息项为传输字典，不添加存储专用 metadata。"""
    return item.model_dump(mode="json", by_alias=True, exclude_none=True) if hasattr(item, "model_dump") else item


@dataclass
class SessionState:
    """同一会话共享历史和锁；运行时计划在会话间独立。"""

    store: SessionStore  # 负责本会话的 JSONL 追加和恢复。
    messages: list[Any] = field(default_factory=list)  # 当前有效模型上下文。
    # todo: 应该持久化锁，后续可能多进程
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # 覆盖整轮执行及历史修改。
    # todo: plan应该仅局限于每个turn
    plan: PlanManager = field(default_factory=PlanManager)  # 当前会话的运行时任务计划。

    @property
    def id(self) -> str:
        """使用 JSONL 文件名中的 UUID 作为会话标识。"""
        return self.store.path.stem


class AgentRuntime:
    """运行时"""

    def __init__(self, config: Config) -> None:
        """保存统一配置，管理 Agent 资源、通道、会话与事件分发的完整生命周期。"""
        self.config = config
        if not config.api_key or not config.model.strip():
            raise ValueError(f"请在 config.yaml 配置 model，并设置 {config.api_key_env} 环境变量")
        self.client = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url)  # 构造客户端，stop 时关闭。
        self.mcp = MCPManager(config)  # 构造管理器，start 时建立连接。
        # todo:
        self.limit = asyncio.Semaphore(config.max_concurrent_requests)  # 请求并发上限。
        self.requests: dict[asyncio.Task, InboundMessage] = {}  # 跟踪已接受的请求，退出时等待完成。
        # todo:具体命名消息分发器
        self.message_dispatcher = MessageDispatcher()
        # todo: ?
        self.approvals = ApprovalBroker(self.message_dispatcher)
        self.sessions: dict[str, SessionState] = {}
        self.default_session_id = ""
        self.channels = ChannelManager(config, self.message_dispatcher)  # 与 Runtime 共享配置和消息分发器。
        self.input_task: asyncio.Task | None = None  # 读取ChannelManager消息任务，记录任务引用stop时方便专门处理
        # 保留本进程创建的后台记忆任务，退出前显式等待并收集异常。
        self.background_tasks: list[asyncio.Task] = []

    def _new(self) -> SessionState:
        """只准备会话路径，首次实际输入才创建 JSONL。"""
        state = SessionState(SessionStore.create(PROJECT_ROOT / SESSION_DIR_NAME))
        self.sessions[state.id] = state
        return state

    def _load(self, session_id: str) -> SessionState:
        """根据受控会话列表查找标识，不接受客户端提供任意文件路径。"""
        if session_id in self.sessions:
            return self.sessions[session_id]
        entry = next((entry for entry in SessionStore.list_sessions(PROJECT_ROOT / SESSION_DIR_NAME)
                      if entry["path"].stem == session_id), None)
        if entry is None:
            raise ValueError("会话不存在，请重新选择。")
        store, messages = SessionStore.load(entry["path"])
        state = SessionState(store, messages)
        self.sessions[state.id] = state
        return state

    async def start(self) -> None:
        """恢复会话，并发启动 MCP 与通道；异常由具体资源模块处理或向外抛出。"""
        entries = SessionStore.list_sessions(PROJECT_ROOT / SESSION_DIR_NAME)
        state = self._load(entries[0]["path"].stem) if entries else self._new()
        self.default_session_id = state.id
        # MCP 连接和通道监听并发启动；完成后再读取输入。
        await asyncio.gather(self.mcp.start(), self.channels.start())
        self.input_task = asyncio.create_task(self._receive_inputs(), name="channel-inputs")
        logger.info("AgentRuntime 已启动：model=%s workdir=%s", self.config.model, self.config.workdir)

    async def _receive_inputs(self) -> None:
        """读取合并后的输入流，处理断线通知并提交消息。"""
        async with aclosing(self.channels.receive()) as inputs:
            async for event in inputs:
                if isinstance(event, ChannelDisconnected):
                    self.approvals.disconnect(event.channel_id, event.reply_target)
                else:
                    self.submit(event)

    async def _snapshot(self, message: InboundMessage, state: SessionState, title: str) -> None:
        """绑定本次回复的会话标识，补齐中断记录并发送历史快照。"""
        async with state.lock:
            state.messages.extend(state.store.recover_pending_tools())
            message.session_id = state.id
            self.default_session_id = state.id
            await self.message_dispatcher.send(message, {
                "type": "session", "title": title, "items": [serializable(item) for item in state.messages],
                "model": self.config.model, "workdir": str(self.config.workdir),
            })
        logger.info("已绑定会话：%s，文件：%s", state.id, state.store.path)

    async def _command(self, message: InboundMessage) -> bool:
        """识别斜杠命令；菜单在客户端展示，文件操作由常驻端执行。"""
        command, _, argument = message.input.partition(" ")
        argument = argument.strip()
        if command in {"/attach", "/new"}:
            state = self._new() if command == "/new" else self._load(message.session_id or self.default_session_id)
            await self._snapshot(message, state, "新会话" if command == "/new" else "已连接会话")
        elif command in {"/resume", "/delete"} and not argument:
            entries = [{"id": entry["path"].stem, "updated_at": entry["updated_at"], "preview": entry["preview"]}
                       for entry in SessionStore.list_sessions(PROJECT_ROOT / SESSION_DIR_NAME)]
            await self.message_dispatcher.send(message, {"type": "sessions", "sessions": entries})
        elif command == "/resume":
            await self._snapshot(message, self._load(argument), "已恢复会话")
        elif command == "/delete":
            session_id, _, confirmation = argument.partition(" ")
            if confirmation != "confirm":
                raise ValueError("删除前需要客户端二次确认。")
            state = self._load(session_id)
            if state.lock.locked() or any(
                task is not asyncio.current_task() and request.session_id == state.id
                for task, request in self.requests.items()
            ):
                raise ValueError("该会话仍有等待或执行中的请求，请稍后删除。")
            SessionStore.delete(state.store.path, PROJECT_ROOT / SESSION_DIR_NAME)
            self.sessions.pop(state.id, None)
            if state.id == self.default_session_id or state.id == message.session_id:
                replacement = self._new()
                if state.id == self.default_session_id:
                    self.default_session_id = replacement.id
                if state.id == message.session_id:
                    await self._snapshot(message, replacement, "当前会话已删除，已开启新会话")
            await self.message_dispatcher.send(message, {"type": "status", "text": "已删除所选会话。"})
            logger.info("已删除会话文件：%s", state.store.path)
        elif command.startswith("/"):
            raise ValueError("未知命令，可用命令：/new、/resume、/delete、/q")
        else:
            return False
        return True

    async def process_event(self, message: InboundMessage, state: SessionState, event: dict[str, Any]) -> None:
        """原始事件先落盘再分发，审批事件在原对象中填写决定。"""
        if event["type"] == "items":
            for item in event["items"]:
                state.store.append(item, model_name=event.get("model_name"))
            await self.message_dispatcher.send(message, {"type": "items", "items": [serializable(item) for item in event["items"]]})
        elif event["type"] == "permission":
            event["allowed"] = await self.approvals.request(message, event)
        else:
            await self.message_dispatcher.send(message, event)

    async def _execute(self, message: InboundMessage) -> None:
        """执行一轮对话，会话锁覆盖落盘、生成器迭代和上下文修改。"""
        state = self._load(message.session_id or self.default_session_id)
        message.session_id = state.id
        async with state.lock:
            state.messages.extend(state.store.recover_pending_tools())
            trigger_hooks("PreUserPromptSubmit", self.config)
            user_message = state.store.start_turn(message.input)
            state.messages.append(user_message)
            await self.message_dispatcher.send(message, {"type": "items", "items": [user_message]})
            # todo：传那么多参数，是否会太乱以及不易维护？配置集中传入，资源由本运行时管理。
            try:
                async with aclosing(agent_loop(self.client, state.messages, message.input,
                                              self.config, self.mcp, state.plan)) as events:
                    async for event in events:
                        await self.process_event(message, state, event)
            except BaseException:
                # 生成器中断后以内存外的原始记录重建历史，下轮再补齐缺失工具结果。
                store, messages = SessionStore.load(state.store.path)
                state.store, state.messages = store, messages
                raise
            # Agent 正常结束后，提取本轮快照，不受后续追加和压缩影响。
            self.background_tasks.append(asyncio.create_task(
                extract_memories(self.client, deepcopy(state.messages), self.config)))

    def submit(self, message: InboundMessage) -> asyncio.Task | None:
        """统一接收输入并启动异步请求；审批直接处理，不占用并发名额。"""
        if message.input.startswith("/approve "):
            self.approvals.resolve(message)
            return None
        # 接受时固定目标会话，后续其他入口切换会话不改变本次请求归属。
        message.session_id = message.session_id or self.default_session_id
        task = asyncio.create_task(self._handle(message), name=f"request-{message.request_id}")
        self.requests[task] = message
        task.add_done_callback(self.requests.pop)
        return task

    async def _handle(self, message: InboundMessage) -> None:
        """在并发限制内直接调用命令或对话处理，接收通道无需等待本轮结束。"""
        try:
            async with self.limit:
                if message.input and not await self._command(message):
                    await self._execute(message)
        except Exception as error:
            logger.exception("请求处理失败：request=%s session=%s", message.request_id, message.session_id)
            await self.message_dispatcher.send(message, {"type": "error", "text": str(error)})
        finally:
            await self.message_dispatcher.send(message, {"type": "done"})

    async def stop(self) -> None:
        """停止通道、完成请求和后台任务后关闭 MCP 和模型客户端，允许启动失败后调用。"""
        try:
            try:
                await self.channels.stop()
            finally:
                try:
                    await self._drain()
                finally:
                    if self.input_task is not None:
                        self.input_task.cancel()
                        await asyncio.gather(self.input_task, return_exceptions=True)
                        self.input_task = None
        finally:
            try:
                await self.mcp.stop()
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
