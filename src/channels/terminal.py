import json
import logging
from typing import Any
from uuid import uuid4

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from src.channels.base import Channel, ChannelDisconnected, InboundMessage


# 终端连接生命周期和协议错误的日志。
logger = logging.getLogger(__name__)


class TerminalChannel(Channel):
    """常驻端的 WebSocket 监听器，终端界面在另一个进程运行。"""

    def __init__(self, channel_id: str,
                 host: str = "127.0.0.1", port: int = 8765) -> None:
        """保存监听地址，连接标识仅由服务端生成。"""
        super().__init__(channel_id)
        self.host = host
        self.port = port
        self.server: Server | None = None
        self.connections: dict[str, ServerConnection] = {}

    @property
    def uri(self) -> str:
        """返回实际监听地址，支持测试使用操作系统分配的随机端口。"""
        port = self.server.sockets[0].getsockname()[1] if self.server else self.port
        return f"ws://{self.host}:{port}"

    async def start(self) -> bool:
        """开启 WebSocket 监听，历史快照不使用默认的一兆字节接收限制。"""
        try:
            self.server = await serve(self._receive, self.host, self.port, max_size=None)
        except OSError:
            logger.exception("终端通道监听失败：channel=%s host=%s port=%s", self.channel_id, self.host, self.port)
            raise
        logger.info("终端通道 %s 已监听 %s", self.channel_id, self.uri)
        return True

    async def _receive(self, connection: ServerConnection) -> None:
        """解析 WebSocket 输入并写入接收缓冲，Runtime 通过 receive() 主动读取。"""
        target = uuid4().hex
        self.connections[target] = connection
        logger.info("终端连接已建立：%s/%s", self.channel_id, target)
        try:
            async for raw in connection:
                try:
                    data = json.loads(raw)
                    text = data["input"]
                    if not isinstance(text, str) or not isinstance(data.get("session_id", ""), str):
                        raise ValueError("input 和 session_id 必须为文本")
                    message = InboundMessage(str(data.get("request_id") or uuid4().hex),
                                             self.channel_id, target, data.get("session_id", ""), text.strip())
                    self._incoming.put_nowait(message)
                except (ValueError, KeyError, TypeError) as error:
                    logger.warning("终端输入无效：%s", error)
                    await self.send(target, {"type": "error", "text": f"输入未接受：{error}"})
                    await self.send(target, {"type": "done"})
        except ConnectionClosed:
            logger.info("终端连接已断开：%s/%s", self.channel_id, target)
        finally:
            self.connections.pop(target, None)
            self._incoming.put_nowait(ChannelDisconnected(self.channel_id, target))

    async def send(self, reply_target: str, event: dict[str, Any]) -> None:
        """将输出事件编码后发送给对应终端连接。"""
        await self.connections[reply_target].send(json.dumps(event, ensure_ascii=False))

    async def stop(self) -> None:
        """关闭监听和全部终端连接，等待接收任务退出。"""
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
