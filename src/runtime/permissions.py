import asyncio
import logging
from typing import Any
from uuid import uuid4

from src.channels.base import Dispatcher, InboundMessage


# 审批请求、答复和断线拒绝的日志。
logger = logging.getLogger(__name__)

# 审批最长等待时间，避免无人操作时永久占用消费者。
APPROVAL_TIMEOUT_SECONDS = 120


class ApprovalBroker:
    """按审批标识匹配答复，控制消息不经过普通输入消费者。"""

    def __init__(self, dispatcher: Dispatcher) -> None:
        """保存待答复项及其来源地址。"""
        self.dispatcher = dispatcher
        self.pending: dict[str, tuple[InboundMessage, asyncio.Future[bool]]] = {}

    async def request(self, message: InboundMessage, event: dict[str, Any]) -> bool:
        """发送审批并等待来源客户端答复，断线或超时均拒绝。"""
        approval_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[approval_id] = (message, future)
        logger.warning("需要权限审批：request=%s tool=%s reason=%s",
                       message.request_id, event["name"], event["reason"])
        try:
            sent = await self.dispatcher.send(message, {
                "type": "approval", "approval_id": approval_id, "name": event["name"],
                "arguments": event["arguments"], "reason": event["reason"],
            })
            if not sent:
                return False
            return await asyncio.wait_for(future, APPROVAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("审批超时，已拒绝：%s", approval_id)
            return False
        finally:
            self.pending.pop(approval_id, None)

    def resolve(self, message: InboundMessage) -> None:
        """只接受原通道和原回复目标对该审批的答复。"""
        parts = message.input.split()
        entry = self.pending.get(parts[1]) if len(parts) == 3 else None
        if entry is None or parts[2] not in {"allow", "deny"}:
            logger.warning("忽略无效审批答复：%s", message.request_id)
            return
        source, future = entry
        if (source.channel_id, source.reply_target) != (message.channel_id, message.reply_target):
            logger.warning("忽略来源不匹配的审批答复：%s", message.request_id)
            return
        if not future.done():
            future.set_result(parts[2] == "allow")
            logger.info("审批 %s 结果：%s", parts[1], parts[2])

    def disconnect(self, channel_id: str, reply_target: str) -> None:
        """连接断开时唤醒并拒绝该连接正在等待的审批。"""
        for source, future in self.pending.values():
            if (source.channel_id, source.reply_target) == (channel_id, reply_target) and not future.done():
                logger.warning("审批连接断开，已拒绝：%s/%s", channel_id, reply_target)
                future.set_result(False)
