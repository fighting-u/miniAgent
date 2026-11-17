import json
import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

from uuid6 import uuid7

from src.session import storage
from src.session.session import Session


# 会话加载、入口绑定和历史管理的诊断日志。
logger = logging.getLogger(__name__)


def serializable(item: Any) -> Any:
    """转换 SDK 消息项为传输字典，不添加存储专用 metadata。"""
    return item.model_dump(mode="json", by_alias=True, exclude_none=True) if hasattr(item, "model_dump") else item


class SessionManager:
    """管理会话状态与入口绑定，JSONL 操作委托给 storage。"""

    def __init__(self, root: Path) -> None:
        """接收会话目录，一次读取绑定，历史按需加载。"""
        self.root = root  # 会话 JSONL 和 bindings 文件所在目录。
        self.sessions: dict[str, Session] = {}  # 已加载或尚未落盘的会话状态。
        self.bindings_path = root / "bindings"  # 稳定通道实例 ID 到会话 ID 的 JSON 映射。
        self.bindings: dict[str, str] = (json.loads(self.bindings_path.read_text(encoding="utf-8"))
                                        if self.bindings_path.exists() else {})  # 只由常驻端修改。
    @staticmethod
    def new_session_id() -> str:
        """只生成会话标识，供 /new 使用，不创建或缓存 Session。"""
        session_id = str(uuid7())
        return session_id

    def get(self, session_id: str) -> Session | None:
        """懒加载获取已有会话"""
        if session_id in self.sessions:
            return self.sessions[session_id]
        entry = next((entry for entry in storage.list_sessions(self.root)
                      if entry["path"].stem == session_id), None)
        if entry is None:
            return None
        session = storage.load(entry["path"])
        self.sessions[session.id] = session
        logger.info("已加载会话：%s", session.id)
        return session

    def get_or_create(self, session_id: str) -> Session:
        """普通对话按 ID 获取或懒创建会话，空 ID 时先生成标识。"""
        session_id = session_id or self.new_session_id()
        session = self.get(session_id)
        if session is None:
            session = storage.create(self.root, session_id)
            self.sessions[session.id] = session
            logger.info("首次输入已准备会话：%s", session.id)
        return session

    def list_sessions(self) -> list[dict[str, Any]]:
        """返回最近写入优先的会话列表，不暴露存储路径。"""
        return [{"id": entry["path"].stem, "updated_at": entry["updated_at"], "preview": entry["preview"]}
                for entry in storage.list_sessions(self.root)]

    async def history(self, session_id: str) -> list[dict[str, Any]]:
        """在现有会话锁内复制历史；空标识返回空白，不创建或修改会话。"""
        if not session_id:
            return []
        session = self.get(session_id)
        async with session.lock:
            return deepcopy([serializable(item) for item in session.messages])

    def get_binding(self, channel_id: str) -> str:
        """读取入口绑定；目标已被删除时清除绑定并恢复为空白。"""
        session_id = self.bindings.get(channel_id, "")
        if session_id and not any(entry["id"] == session_id for entry in self.list_sessions()):
            logger.warning("入口绑定的会话已不存在：channel=%s session=%s", channel_id, session_id)
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

    def start_turn(self, session: Session, text: str) -> dict[str, Any]:
        """由调用方持锁，补齐中断工具结果并保存新的用户输入。"""
        session.messages.extend(storage.recover_pending_tools(session))
        message = storage.start_turn(session, text)
        session.messages.append(message)
        return message

    def append(self, session: Session, item: Any, model_name: str | None = None) -> None:
        """由调用方持锁，委托 storage 保存原始模型或工具消息。"""
        storage.append(session, item, model_name=model_name)

    def reload(self, session: Session) -> None:
        """执行中断后从已落盘记录重建同一会话的状态，保留原有锁。"""
        loaded = storage.load(session.path)
        session.messages = loaded.messages
        session.ordinal = loaded.ordinal
        session.turn_id = loaded.turn_id
        session.persisted = loaded.persisted
        session.pending_calls = loaded.pending_calls

    def delete(self, session_id: str) -> None:
        """删除没有执行中任务的会话，并清除所有指向该会话的持久化绑定。"""
        session = self.get(session_id)
        if session.lock.locked():
            raise ValueError("该会话仍有等待或执行中的请求，请稍后删除。")
        if session.persisted:
            storage.delete(session.path, self.root)
        self.sessions.pop(session_id)
        for channel_id, selected in list(self.bindings.items()):
            if selected == session_id:
                self.bind(channel_id, "")
        logger.info("已删除会话：%s", session_id)
