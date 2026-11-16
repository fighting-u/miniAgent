import asyncio
import logging
from typing import Any, AsyncIterator

from src.channels.base import Channel, ChannelDisconnected, InboundMessage, MessageDispatcher
from src.config import Config
from src.channels.qq import QQChannel
from src.channels.terminal import TerminalChannel


# 通道配置与实例启动的诊断日志。
logger = logging.getLogger(__name__)

# 类型注册表只保存创建方法，运行实例由 MessageDispatcher 单独管理。
CHANNEL_TYPES: dict[str, type[Channel]] = {"terminal": TerminalChannel, "qq": QQChannel}


class ChannelManager:
    """维护通道生命周期，并对外提供合并后的输入流。"""

    def __init__(self, config: Config, message_dispatcher: MessageDispatcher) -> None:
        """保存共享配置与消息分发器，管理通道生命周期和输入合并。"""
        self.config = config
        self.message_dispatcher = message_dispatcher

    async def receive(self) -> AsyncIterator[InboundMessage | ChannelDisconnected]:
        """并发等待各通道的下一条输入，按到达情况合并产出。"""
        # 每个通道只保留一个读取任务，任务生命周期限定在本次输入流内。
        pending = {}
        for channel in self.message_dispatcher.channels.values():
            stream = channel.receive()
            pending[asyncio.create_task(anext(stream))] = stream
        # TODO：后续补充运行中增删通道的输入流同步，以及单个通道的读取异常隔离。
        try:
            while pending:
                completed, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in completed:
                    stream = pending.pop(task)
                    try:
                        event = task.result()
                    except StopAsyncIteration:
                        continue
                    pending[asyncio.create_task(anext(stream))] = stream
                    yield event
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def add(self, settings: dict[str, Any]) -> None:
        """创建并启动一个实例，成功后才发布到分发表。"""
        channel_id = settings["id"]
        if channel_id in self.message_dispatcher.channels:
            raise ValueError(f"通道实例已存在：{channel_id}")
        if settings["type"] not in CHANNEL_TYPES:
            raise ValueError(f"未知通道类型：{settings['type']}")
        channel = CHANNEL_TYPES[settings["type"]](channel_id,
                                                   **settings.get("options", {}))
        if await channel.start():
            self.message_dispatcher.channels[channel_id] = channel
            print(f"通道已就绪：{channel.channel_id} {getattr(channel, 'uri', '')}", flush=True)

    async def start(self) -> None:
        """启动启用的通道，由各通道决定连接失败时跳过或向外抛出。"""
        for item in self.config.channels:
            if item.get("enabled", True):
                await self.add(item)

    async def remove(self, channel_id: str) -> None:
        """关闭一个实例，再从输出注册表移除。"""
        channel = self.message_dispatcher.channels[channel_id]
        await channel.stop()
        del self.message_dispatcher.channels[channel_id]

    async def stop(self) -> None:
        """关闭已注册实例，单个实例关闭失败仍继续清理其余实例。"""
        for channel_id in list(self.message_dispatcher.channels):
            try:
                await self.remove(channel_id)
            except Exception:
                logger.exception("通道关闭失败：%s", channel_id)
