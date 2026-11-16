import logging
from typing import Any

from src.channels.base import Channel, MessageDispatcher, Submit
from src.config import Config
from src.channels.qq import QQChannel
from src.channels.terminal import TerminalChannel


# 通道配置与实例启动的诊断日志。
logger = logging.getLogger(__name__)

# 类型注册表只保存创建方法，运行实例由 MessageDispatcher 单独管理。
CHANNEL_TYPES: dict[str, type[Channel]] = {"terminal": TerminalChannel, "qq": QQChannel}


class ChannelManager:
    """维护通道生命周期，创建实例时传入统一提交回调。"""

    def __init__(self, config: Config, message_dispatcher: MessageDispatcher, submit: Submit) -> None:
        """保存共享配置、消息分发器与提交回调，管理通道生命周期。"""
        self.config = config
        self.message_dispatcher = message_dispatcher
        self.submit = submit  # 原样传给通道，不在管理器中处理请求。

    async def add(self, settings: dict[str, Any]) -> None:
        """启动前注册实例以支持即时回复，启动失败时移除注册。"""
        channel_id = settings["id"]
        if channel_id in self.message_dispatcher.channels:
            raise ValueError(f"通道实例已存在：{channel_id}")
        if settings["type"] not in CHANNEL_TYPES:
            raise ValueError(f"未知通道类型：{settings['type']}")
        channel = CHANNEL_TYPES[settings["type"]](channel_id, self.submit,
                                                **settings.get("options", {}))
        # 通道在 start 返回前就可能提交输入，先注册才能正确分发首条回复。
        self.message_dispatcher.channels[channel_id] = channel
        started = False
        try:
            started = await channel.start()
        finally:
            if not started:
                del self.message_dispatcher.channels[channel_id]
        if started:
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
