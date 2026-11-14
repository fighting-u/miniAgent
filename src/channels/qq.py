from typing import Any

from src.channels.base import Channel


class QQChannel(Channel):
    """QQ 通道占位，后续接入实际平台协议。"""

    async def start(self) -> None:
        """明确提示未实现，避免把占位实例注册为可用通道。"""
        raise NotImplementedError("QQ 通道尚未接入，请在配置中保持禁用。")

    async def send(self, reply_target: str, event: dict[str, Any]) -> None:
        """预留 QQ 目标发送接口。"""
        raise NotImplementedError("QQ 消息发送尚未实现。")

    async def stop(self) -> None:
        """占位通道没有需要关闭的资源。"""
