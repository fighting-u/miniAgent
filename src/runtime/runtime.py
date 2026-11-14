import asyncio
import logging
import os
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.agent.hook import trigger_hooks
from src.channels.base import Dispatcher, InboundMessage
from src.runtime.permissions import ApprovalBroker
from src.runtime.runner import Runner
from src.session.session_store import SESSION_DIR, SessionStore
from src.tools.tools import PlanManager


# 会话协调、消费者和持久化的日志。
logger = logging.getLogger(__name__)


def serializable(item: Any) -> Any:
    """转换 SDK 消息项为传输字典，不添加存储专用 metadata。"""
    return item.model_dump(mode="json", by_alias=True, exclude_none=True) if hasattr(item, "model_dump") else item


@dataclass
class SessionState:
    """同一会话共享历史和锁；运行时计划在会话间独立。"""

    store: SessionStore  # 负责本会话的 JSONL 追加和恢复。
    messages: list[Any] = field(default_factory=list)  # 当前有效模型上下文。
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # 覆盖整轮执行及历史修改。
    plan: PlanManager = field(default_factory=PlanManager)  # 当前会话的运行时任务计划。

    @property
    def id(self) -> str:
        """使用 JSONL 文件名中的 UUID 作为会话标识。"""
        return self.store.path.stem


class Runtime:
    """单消费者运行协调层，不依赖终端界面和具体平台协议。"""

    def __init__(self, runner: Runner, session_root: Path = SESSION_DIR, queue_size: int = 100) -> None:
        """初始化队列、会话表和输出分发，保留可替换执行器。"""
        self.runner = runner
        self.session_root = session_root.resolve()
        self.queue: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=queue_size)
        self.dispatcher = Dispatcher()
        self.approvals = ApprovalBroker(self.dispatcher)
        self.sessions: dict[str, SessionState] = {}
        self.default_session_id = ""
        self.worker: asyncio.Task | None = None
        # 保留本进程创建的后台记忆任务，退出前显式等待并收集异常。
        self.background_tasks: list[asyncio.Task] = []

    def _new(self) -> SessionState:
        """只准备会话路径，首次实际输入才创建 JSONL。"""
        state = SessionState(SessionStore.create(self.session_root))
        self.sessions[state.id] = state
        return state

    def _load(self, session_id: str) -> SessionState:
        """根据受控会话列表查找标识，不接受客户端提供任意文件路径。"""
        if session_id in self.sessions:
            return self.sessions[session_id]
        entry = next((entry for entry in SessionStore.list_sessions(self.session_root)
                      if entry["path"].stem == session_id), None)
        if entry is None:
            raise ValueError("会话不存在，请重新选择。")
        store, messages = SessionStore.load(entry["path"])
        state = SessionState(store, messages)
        self.sessions[state.id] = state
        return state

    async def start(self) -> None:
        """恢复最近会话，启动唯一输入消费者。"""
        entries = SessionStore.list_sessions(self.session_root)
        try:
            state = self._load(entries[0]["path"].stem) if entries else self._new()
        except (OSError, ValueError):
            logger.exception("最近会话无法恢复，已准备新会话，原文件保留")
            state = self._new()
        self.default_session_id = state.id
        self.worker = asyncio.create_task(self._consume(), name="miniagent-consumer")

    async def _snapshot(self, message: InboundMessage, state: SessionState, title: str) -> None:
        """绑定本次回复的会话标识，补齐中断记录并发送历史快照。"""
        async with state.lock:
            state.messages.extend(state.store.recover_pending_tools())
            message.session_id = state.id
            self.default_session_id = state.id
            await self.dispatcher.send(message, {
                "type": "session", "title": title, "items": [serializable(item) for item in state.messages],
                "model": os.getenv("MODEL", ""), "workdir": str(Path.cwd()),
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
                       for entry in SessionStore.list_sessions(self.session_root)]
            await self.dispatcher.send(message, {"type": "sessions", "sessions": entries})
        elif command == "/resume":
            await self._snapshot(message, self._load(argument), "已恢复会话")
        elif command == "/delete":
            session_id, _, confirmation = argument.partition(" ")
            if confirmation != "confirm":
                raise ValueError("删除前需要客户端二次确认。")
            state = self._load(session_id)
            if state.lock.locked() or not self.queue.empty():
                raise ValueError("仍有排队或执行中的请求，请稍后删除。")
            SessionStore.delete(state.store.path, self.session_root)
            self.sessions.pop(state.id, None)
            if state.id == self.default_session_id or state.id == message.session_id:
                replacement = self._new()
                if state.id == self.default_session_id:
                    self.default_session_id = replacement.id
                if state.id == message.session_id:
                    await self._snapshot(message, replacement, "当前会话已删除，已开启新会话")
            await self.dispatcher.send(message, {"type": "status", "text": "已删除所选会话。"})
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
            await self.dispatcher.send(message, {"type": "items", "items": [serializable(item) for item in event["items"]]})
        elif event["type"] == "permission":
            event["allowed"] = await self.approvals.request(message, event)
        else:
            await self.dispatcher.send(message, event)

    async def _execute(self, message: InboundMessage) -> None:
        """执行一轮对话，会话锁覆盖落盘、生成器迭代和上下文修改。"""
        state = self._load(message.session_id or self.default_session_id)
        message.session_id = state.id
        async with state.lock:
            state.messages.extend(state.store.recover_pending_tools())
            trigger_hooks("PreUserPromptSubmit")
            user_message = state.store.start_turn(message.input)
            state.messages.append(user_message)
            await self.dispatcher.send(message, {"type": "items", "items": [user_message]})
            # todo：传那么多参数，是否会太乱以及不易维护？运行资源已收拢到 Runner。
            try:
                async with aclosing(self.runner.run(state.messages, message.input, state.plan)) as events:
                    async for event in events:
                        await self.process_event(message, state, event)
            except BaseException:
                # 生成器中断后以内存外的原始记录重建历史，下轮再补齐缺失工具结果。
                store, messages = SessionStore.load(state.store.path)
                state.store, state.messages = store, messages
                raise
            # Agent 正常结束后，提取本轮快照，不受后续追加和压缩影响。
            self.background_tasks.append(asyncio.create_task(self.runner.extract(deepcopy(state.messages))))

    async def _consume(self) -> None:
        """顺序消费输入；执行入口独立，后续可替换为执行池调度。"""
        while True:
            message = await self.queue.get()
            try:
                if message.input and not await self._command(message):
                    await self._execute(message)
            except Exception as error:
                logger.exception("请求处理失败：request=%s session=%s", message.request_id, message.session_id)
                await self.dispatcher.send(message, {"type": "error", "text": str(error)})
            finally:
                await self.dispatcher.send(message, {"type": "done"})
                self.queue.task_done()

    async def stop(self) -> None:
        """结束消费者，收集所有后台记忆任务；模型资源由启动入口关闭。"""
        if self.worker is not None:
            await self.queue.join()
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
            self.worker = None
        if self.background_tasks:
            results = await asyncio.gather(*self.background_tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    logger.warning("后台记忆提取任务未正常完成：%s", result)
            self.background_tasks.clear()
