from typing import Any

from src.channels.base import InboundMessage, MessageDispatcher
from src.session.session import Session
from src.session.session_manager import SessionManager, serializable


async def persist_and_dispatch_items(
    inbound_message: InboundMessage, session: Session, items: list[Any],
    session_manager: SessionManager, message_dispatcher: MessageDispatcher,
    *, model_name: str | None = None,
) -> None:
    """按原顺序落盘消息条目，再序列化并发送给来源通道。"""
    for item in items:
        session_manager.append(session, item, model_name=model_name)
    await message_dispatcher.send(
        inbound_message, {"type": "items", "items": [serializable(item) for item in items]},
    )
