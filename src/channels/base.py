import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


# 通道注册和输出分发使用的诊断日志。
logger = logging.getLogger(__name__)


@dataclass
class InboundMessage:
    """通道转换后的统一输入，回复地址由接收通道绑定。"""

    request_id: str  # 本次输入的请求标识。
    channel_id: str  # 接收输入的通道实例标识。
    reply_target: str  # 通道内部的连接或聊天目标。
    session_id: str  # 本次输入所属会话；首次连接时允许为空。
    input: str  # 普通问题或斜杠命令。


class Channel(ABC):
    """通道统一接口，不限定主动连接或被动监听。"""

    def __init__(self, channel_id: str, queue: asyncio.Queue[InboundMessage], approvals: Any) -> None:
        """保存实例标识、公共输入队列和审批答复入口。"""
        self.channel_id = channel_id
        self.queue = queue
        self.approvals = approvals

    @abstractmethod
    async def start(self) -> None:
        """启动协议连接或监听。"""

    @abstractmethod
    async def send(self, reply_target: str, event: dict[str, Any]) -> None:
        """向本通道内的指定目标发送事件。"""

    @abstractmethod
    async def stop(self) -> None:
        """关闭连接并清理通道资源。"""


class Dispatcher:
    """按通道实例标识查找发送实现。"""

    def __init__(self) -> None:
        """创建已就绪通道实例的注册表。"""
        self.channels: dict[str, Channel] = {}

    async def send(self, message: InboundMessage, event: dict[str, Any]) -> bool:
        """补充请求关联信息并发送；断线不会向上中断 Agent。"""
        envelope = {**event, "request_id": message.request_id, "session_id": message.session_id,
                    "channel_id": message.channel_id, "reply_target": message.reply_target}
        try:
            await self.channels[message.channel_id].send(message.reply_target, envelope)
            return True
        except Exception:
            logger.warning("事件发送失败：request=%s session=%s channel=%s target=%s",
                           message.request_id, message.session_id, message.channel_id,
                           message.reply_target, exc_info=True)
            return False
