import asyncio
import logging
from pathlib import Path
from typing import Any

import yaml

from src.channels.base import Channel, Dispatcher
from src.channels.qq import QQChannel
from src.channels.terminal import TerminalChannel


# 通道配置与实例启动的诊断日志。
logger = logging.getLogger(__name__)

# 类型注册表只保存创建方法，运行实例由 Dispatcher 单独管理。
CHANNEL_TYPES: dict[str, type[Channel]] = {"terminal": TerminalChannel, "qq": QQChannel}

# 配置路径固定在项目根目录，不随客户端工作目录变化。
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / ".channels.yaml"


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    """读取并检查通道配置，暂不监听配置文件变化。"""
    settings = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(settings, dict) or not isinstance(settings.get("channels"), list):
        raise ValueError("通道配置必须包含 channels 列表")
    seen = set()
    for item in settings["channels"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError("每个通道必须配置有效的 id")
        if item["id"] in seen or item.get("type") not in CHANNEL_TYPES:
            raise ValueError(f"通道 ID 重复或类型未知：{item['id']}")
        seen.add(item["id"])
    size = settings.get("queue_size", 100)
    if type(size) is not int or size <= 0:
        raise ValueError("queue_size 必须为正整数")
    return settings


class ChannelManager:
    """维护实例生命周期，后续配置热更新复用 add 和 remove。"""

    def __init__(self, queue: asyncio.Queue, approvals: Any, dispatcher: Dispatcher) -> None:
        """共享输入队列和运行实例注册表。"""
        self.queue = queue
        self.approvals = approvals
        self.dispatcher = dispatcher

    async def add(self, settings: dict[str, Any]) -> None:
        """创建并启动一个实例，成功后才发布到分发表。"""
        channel_id = settings["id"]
        if channel_id in self.dispatcher.channels:
            raise ValueError(f"通道实例已存在：{channel_id}")
        channel = CHANNEL_TYPES[settings["type"]](channel_id, self.queue, self.approvals,
                                                   **settings.get("options", {}))
        try:
            await channel.start()
        except BaseException:
            await channel.stop()
            logger.exception("通道启动失败：%s", channel_id)
            raise
        self.dispatcher.channels[channel_id] = channel

    async def start(self, settings: dict[str, Any]) -> None:
        """启动配置中启用的通道，部分失败时回收已启动实例。"""
        try:
            for item in settings["channels"]:
                if item.get("enabled", True):
                    await self.add(item)
        except BaseException:
            await self.stop()
            raise

    async def remove(self, channel_id: str) -> None:
        """关闭一个实例，再从输出注册表移除。"""
        channel = self.dispatcher.channels[channel_id]
        await channel.stop()
        del self.dispatcher.channels[channel_id]

    async def stop(self) -> None:
        """关闭已注册实例，单个实例关闭失败仍继续清理其余实例。"""
        for channel_id in list(self.dispatcher.channels):
            try:
                await self.remove(channel_id)
            except Exception:
                logger.exception("通道关闭失败：%s", channel_id)
