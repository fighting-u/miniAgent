import json
import logging
import math
from copy import deepcopy
from pathlib import Path
from time import monotonic
from typing import Any

from uuid6 import uuid7

from src.prompt.developer import MEMORY_CATALOG_PROMPT, RECALLED_MEMORY_PROMPT, SESSION_PROMPT
from src.channels.base import InboundMessage, MessageDispatcher
from src.common.common import extract_total_tokens
from src.config import PROJECT_ROOT, Config
from src.memory.memory import memory_catalog
from src.models.registry import get_model_info
from src.session import storage
from src.session.session import Session
from src.tools.skill import SKILLS_DIR_NAME, SkillLoader

# 会话加载、入口绑定和历史管理的诊断日志。
logger = logging.getLogger(__name__)


def serializable(item: Any) -> Any:
    """转换 SDK 消息项为传输字典，不添加存储专用 metadata。"""
    return item.model_dump(mode="json", by_alias=True, exclude_none=True) if hasattr(item, "model_dump") else item


class SessionManager:
    """管理会话状态与入口绑定，JSONL 操作委托给 storage。"""

    def __init__(self, root: Path, config: Config, message_dispatcher: MessageDispatcher) -> None:
        """接收会话目录和配置，保存占用保留秒数，一次读取绑定，历史按需加载。"""
        self.root = root  # 会话 JSONL 和 bindings 文件所在目录。
        self.config = config  # 构建会话开头的目录权限和长期记忆说明。
        self.message_dispatcher = message_dispatcher  # 将已落盘的消息分发给来源通道。
        # 启动时校验默认模型已声明上下文窗口，供新建或加载会话初始化 model_info。
        self.default_model_info = get_model_info(config.model)
        # 写入和恢复都接受基础消息，以及配置中各托管工具对应的调用记录。
        self.message_types = storage.BASE_MESSAGE_TYPES.union(f"{name}_call" for name in config.model_hosted_tools)
        self.session_owner_ttl = config.session_owner_ttl  # 当前轮结束后，原来源继续占用的秒数。
        self.sessions: dict[str, Session] = {}  # 已加载或尚未落盘的会话状态。
        self.bindings_path = root / "bindings"  # 稳定通道实例 ID 到会话 ID 的 JSON 映射。
        self.bindings: dict[str, str] = (
            json.loads(self.bindings_path.read_text(encoding="utf-8")) if self.bindings_path.exists() else {}
        )  # 只由常驻端修改。

    @staticmethod
    def new_session_id() -> str:
        """只生成会话标识，供 /new 使用，不创建或缓存 Session。"""
        session_id = str(uuid7())
        return session_id

    def get(self, session_id: str) -> Session | None:
        """懒加载获取已有会话"""
        if session_id in self.sessions:
            return self.sessions[session_id]
        entry = next((entry for entry in storage.list_sessions(self.root) if entry["path"].stem == session_id), None)
        if entry is None:
            return None
        session = storage.load(entry["path"], message_types=self.message_types)
        session.model_info = self.default_model_info
        self.sessions[session.id] = session
        logger.info("已加载会话：%s", session.id)
        return session

    def _developer_messages(self) -> list[dict[str, str]]:
        """为新会话或压缩后的历史生成当前环境、技能和可选的记忆目录快照。"""
        cwd = Path.cwd().resolve()
        allowed_dirs = "、".join(str(path) for path in dict.fromkeys((cwd, *self.config.allowed_dirs)))
        content = SESSION_PROMPT.format(
            cwd=cwd, allowed_dirs=allowed_dirs, skills=SkillLoader(PROJECT_ROOT / SKILLS_DIR_NAME).catalog()
        )
        messages = [{"type": "message", "role": "developer", "content": content}]
        if self.config.long_term_memory_enabled:
            messages.append({"type": "message", "role": "developer", "content": MEMORY_CATALOG_PROMPT.format(catalog=memory_catalog())})
        return messages

    def get_or_create(self, session_id: str) -> Session:
        """普通对话按 ID 获取或懒创建会话，空 ID 时先生成标识。"""
        session_id = session_id or self.new_session_id()
        session = self.get(session_id)
        if session is None:
            session = storage.create(self.root, session_id)
            session.messages.extend(self._developer_messages())
            session.model_info = self.default_model_info
            self.sessions[session.id] = session
            logger.info("首次输入已准备会话：%s", session.id)
        return session

    def list_sessions(self) -> list[dict[str, Any]]:
        """返回最近写入优先的会话列表，不暴露存储路径。"""
        return [
            {"id": entry["path"].stem, "updated_at": entry["updated_at"], "preview": entry["preview"]} for entry in storage.list_sessions(self.root)
        ]

    def try_acquire_session_lock(self, session: Session, requester_id: str) -> bool:
        """同步判断并登记占用；执行中（含同来源）或其他来源未过期时直接拒绝，不等待。"""
        if math.isinf(session.expires_at):
            return False
        if session.owner_id and session.owner_id != requester_id and session.expires_at > monotonic():
            return False
        if session.owner_id != requester_id:
            logger.info("会话session_id=%s 由 requester_id=%s 占用", session.id, requester_id)
        session.owner_id = requester_id
        session.expires_at = float("inf")
        return True

    def release_session_lock(self, session: Session) -> None:
        """当前轮正常结束后设置保留期，不覆盖其他来源的占用。"""
        session.expires_at = monotonic() + self.session_owner_ttl
        logger.info("会话执行结束，保留占用 %s 秒：session_id=%s owner_id=%s", self.session_owner_ttl, session.id, session.owner_id)

    async def history(self, session_id: str) -> list[dict[str, Any]]:
        """直接复制历史快照；空标识返回空白，不创建或修改会话。"""
        if not session_id:
            return []
        session = self.get(session_id)
        return deepcopy([serializable(item) for item in session.messages])

    def get_binding(self, channel_id: str) -> str:
        """读取入口绑定；目标已被删除时清除绑定并恢复为空白。"""
        session_id = self.bindings.get(channel_id, "")
        if session_id and not any(entry["id"] == session_id for entry in self.list_sessions()):
            logger.warning("入口绑定的会话已不存在：channel_id=%s session_id=%s", channel_id, session_id)
            self.bind(channel_id, "")
            return ""
        return session_id

    def bind(self, channel_id: str, session_id: str) -> None:
        """保存入口选择，通过临时文件替换；空字符串表示下次打开空白会话。"""
        if self.bindings.get(channel_id) == session_id:
            return
        bindings = {**self.bindings, channel_id: session_id}
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.bindings_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(bindings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.bindings_path)
        self.bindings = bindings
        logger.info("已保存入口会话绑定：channel=%s session=%s", channel_id, session_id)

    def recover_pending_tools(self, session: Session) -> list[dict[str, Any]]:
        """由调用方取得会话占用，补齐中断工具调用结果并追加到内存历史。"""
        if not session.pending_calls:
            return []
        results = storage.recover_pending_tools(session)
        session.messages.extend(results)
        return results

    def append_turn_input(self, session: Session, user_message: dict[str, str], recalled_memories: str) -> None:
        """同轮落盘召回内容和用户输入；首次输入一并写入暂存在内存中的会话前缀。"""
        items = []
        if recalled_memories:
            content = RECALLED_MEMORY_PROMPT.format(memories=recalled_memories)
            items.append({"type": "message", "role": "developer", "content": content})
        items.append(user_message)
        storage.persist_turn_input(session, items if session.persisted else [*session.messages, *items])
        session.messages.extend(items)

    async def persist_and_dispatch_items(
        self, inbound_message: InboundMessage, session: Session, items: list[Any],
        *, model_name: str | None = None, is_error: bool | None = None, output_preview: str | None = None,
    ) -> None:
        """由调用方取得会话占用，按原顺序批量落盘消息条目，再序列化并发送给来源通道。"""
        storage.append_items(session, items, model_name=model_name, is_error=is_error, message_types=self.message_types)
        event_items = [
            {**item, "output": output_preview} if output_preview is not None else serializable(item)
            for item in items
        ]
        await self.message_dispatcher.send(inbound_message, {"type": "items", "items": event_items})

    def record_usage(self, session: Session, source: str, usage: Any) -> None:
        """持久化响应实际报告的 token 用量并更新内存基准与累计数；缺 usage 时不编造记录。"""
        request_total_tokens = extract_total_tokens(usage)
        if request_total_tokens is None:
            return
        storage.append_token_usage(session, source=source, request_total_tokens=request_total_tokens)
        session.token_usage.total_reported_tokens += request_total_tokens
        # 分正常请求(main) 和 压缩compact请求 2类，compact就更新上次普通主响应的 token 数和已计算的消息数
        if source == "main":
            session.token_usage.last_total_tokens = request_total_tokens
            session.token_usage.covered_items = len(session.messages)

    def install_compacted(self, session: Session, replacement: list[dict[str, Any]], summary_tokens: int | None) -> None:
        """先向 JSONL 提交 compacted 检查点，落盘成功后原地替换内存历史并清空旧主基准。"""
        new_total = session.token_usage.total_reported_tokens + (summary_tokens or 0)
        replacement = [*self._developer_messages(), *replacement]
        storage.append_compacted(session, replacement, summary_tokens=summary_tokens, total_reported_tokens=new_total)
        session.messages[:] = replacement
        session.summary_index = len(replacement) - 1
        session.token_usage.last_total_tokens = None
        session.token_usage.covered_items = 0
        session.token_usage.total_reported_tokens = new_total

    def reload(self, session: Session) -> None:
        """执行中断后从已落盘记录重建同一会话的状态，保留来源占用信息；未落盘会话直接跳过。"""
        if not session.persisted:
            return
        loaded = storage.load(session.path, message_types=self.message_types)
        session.messages[:] = loaded.messages
        session.ordinal = loaded.ordinal
        session.turn_id = loaded.turn_id
        session.persisted = loaded.persisted
        session.pending_calls = loaded.pending_calls
        session.token_usage = loaded.token_usage
        session.summary_index = loaded.summary_index

    def delete(self, session_id: str) -> None:
        """删除没有执行中任务的会话，并清除所有指向该会话的持久化绑定。"""
        session = self.get(session_id)
        if session.persisted:
            storage.delete(session.path, self.root)
        self.sessions.pop(session_id)
        for channel_id, selected in list(self.bindings.items()):
            if selected == session_id:
                self.bind(channel_id, "")
        logger.info("已删除会话：%s", session_id)
