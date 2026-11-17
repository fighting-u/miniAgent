import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.client import connect

from src.channels.base import Channel, ChannelDisconnected, InboundMessage, Submit, input_type


# QQ 鉴权、网关生命周期和私聊消息转换的诊断日志；不记录凭据和请求头。
logger = logging.getLogger(__name__)


@dataclass
class QQReply:
    """保留原始消息的被动回复凭据和本轮最终回答。"""

    target: str  # QQ 用户 openid，作为稳定的 reply_target。
    msg_id: str  # 用户原始消息 ID，发送被动回复时必须携带。
    seq: int = 0  # 同一原始消息的回复序号，避免重复 msg_id + msg_seq。
    texts: list[str] = field(default_factory=list)  # 暂存本轮助手文本，done 时合并发送。


class QQChannel(Channel):
    """连接 QQ 官方网关，只处理 QQ 好友私聊，不接收群聊或频道消息。"""

    def __init__(self, channel_id: str, submit: Submit,
                 app_id: str = "", secret: str = "",
                 is_sandbox: bool = False, startup_timeout: float = 45, session_id: str = "") -> None:
        """接收 Config 已解析的凭据；SDK 负责鉴权与 REST，通道管理网关任务。"""
        super().__init__(channel_id, submit)
        self.session_id = session_id  # 启动时恢复该通道的绑定，后续由明确的 session 事件更新。
        self.app_id = app_id
        self.secret = secret
        self.is_sandbox = is_sandbox
        self.startup_timeout = startup_timeout
        self.http: Any = None  # SDK HTTP 客户端，停止时显式关闭。
        self.api: Any = None  # SDK 消息发送与网关地址查询接口。
        self.robot: dict[str, Any] = {}  # 平台返回的机器人资料，不包含密钥。
        self.task: asyncio.Task | None = None  # 本通道唯一网关连接及重连任务。
        self.ready = asyncio.Event()  # 首次收到 READY 后才视为启动成功。
        self.heartbeat_ack = asyncio.Event()  # 最近一次心跳确认，供连接测试验证。
        self.connected = False  # 当前网关是否完成鉴权。
        self.replies: dict[str, QQReply] = {}  # 请求关联原始 QQ 消息，done 后释放。

    async def _login(self) -> None:
        """延迟加载官方 SDK，避免未启用 QQ 时改变终端进程的日志配置。"""
        from botpy.api import BotAPI
        from botpy.http import BotHttp
        from botpy.robot import Token

        app_id = self.app_id
        secret = self.secret
        if not app_id or not secret:
            raise ValueError("QQ 凭据未配置，请检查 config.yaml 中指定的 AppID 和 AppSecret 环境变量")
        # SDK 的 DEBUG 日志包含鉴权头，固定使用 INFO，并复用项目日志文件。
        logging.getLogger("botpy").setLevel(logging.INFO)
        self.http = BotHttp(timeout=15, is_sandbox=self.is_sandbox)
        self.api = BotAPI(self.http)
        token = Token(app_id, secret)
        await token.check_token()
        logger.info("QQ 访问令牌获取成功，正在查询机器人资料：channel=%s", self.channel_id)
        self.robot = await self.http.login(token)
        logger.info("QQ REST 鉴权成功：channel=%s app_id=%s", self.channel_id, app_id)

    async def start(self) -> bool:
        """等待真实网关 READY；连接失败记录日志并返回 False，不中断其他通道启动。"""
        self.ready.clear()
        self.heartbeat_ack.clear()
        self.task = asyncio.create_task(self._run(), name=f"qq-gateway-{self.channel_id}")
        waiter = asyncio.create_task(self.ready.wait())
        try:
            done, _ = await asyncio.wait({self.task, waiter}, timeout=self.startup_timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if self.task in done:
                await self.task
                raise RuntimeError("QQ 网关在就绪前退出")
            if waiter not in done:
                raise TimeoutError("等待 QQ 网关 READY 超时")
        except asyncio.CancelledError:
            await self.stop()
            raise
        except Exception:
            logger.exception("QQ 通道连接失败，跳过该实例：%s", self.channel_id)
            await self.stop()
            return False
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        return True

    async def _run(self) -> None:
        """首次失败直接上报；已经就绪后断开则重新获取网关并连接。"""
        await self._login()
        while True:
            try:
                await self._connect()
            except Exception:
                if not self.ready.is_set():
                    raise
                logger.warning("QQ 网关断开，稍后重连：%s", self.channel_id, exc_info=True)
            await asyncio.sleep(3)

    async def _heartbeat(self, socket: Any, interval: float, sequence: list[int | None]) -> None:
        """按网关 Hello 指定的间隔发心跳；缺少 ACK 时关闭连接触发重连。"""
        while True:
            self.heartbeat_ack.clear()
            await socket.send(json.dumps({"op": 1, "d": sequence[0]}))
            try:
                await asyncio.wait_for(self.heartbeat_ack.wait(), interval)
            except asyncio.TimeoutError:
                logger.warning("QQ 心跳未确认，关闭连接：%s", self.channel_id)
                await socket.close(code=4000, reason="heartbeat timeout")
                return
            await asyncio.sleep(interval)

    async def _connect(self) -> None:
        """使用 SDK 查询官方网关，通道自行管理 WebSocket 与心跳的关闭范围。"""
        logger.info("正在获取 QQ 网关地址：channel=%s", self.channel_id)
        gateway = await self.api.get_ws_url()
        if not isinstance(gateway, dict) or not gateway.get("url"):
            raise RuntimeError("QQ 未返回有效网关地址")
        if gateway.get("shards", 1) != 1:
            raise RuntimeError("当前 QQ 通道只支持单分片机器人")
        logger.info("正在连接 QQ WebSocket 网关：channel=%s", self.channel_id)
        heartbeat = None
        interval = None  # Hello 下发后保存，READY 后才启动业务心跳。
        # SDK 1.x 的 Client.close 不回收网关与心跳，因此由本通道显式管理。
        async with connect(gateway["url"], ping_interval=None, proxy=None, max_size=None) as socket:
            sequence: list[int | None] = [None]  # 心跳携带最近收到的事件序号。
            try:
                async for raw in socket:
                    payload = json.loads(raw)
                    if payload.get("s") is not None:
                        sequence[0] = payload["s"]
                    opcode = payload["op"]
                    logger.debug("QQ 网关事件：channel=%s opcode=%s type=%s", self.channel_id, opcode, payload.get("t"))
                    if opcode == 10:
                        await self.http.check_session()
                        interval = payload["d"]["heartbeat_interval"] / 1000
                        if interval <= 0:
                            raise ValueError("QQ 心跳间隔必须大于零")
                        await socket.send(json.dumps({"op": 2, "d": {
                            "token": self.http._token.get_string(),
                            "intents": 1 << 25, "shard": [0, 1],
                        }}))
                    elif opcode == 11:
                        self.heartbeat_ack.set()
                    elif opcode in {7, 9}:
                        raise ConnectionError(f"QQ 网关要求重新连接：opcode={opcode}")
                    elif opcode == 0 and payload.get("t") == "READY":
                        if interval is None:
                            raise ValueError("QQ READY 之前未收到 Hello")
                        if heartbeat is None:
                            heartbeat = asyncio.create_task(self._heartbeat(socket, interval, sequence))
                        self.connected = True
                        self.ready.set()
                        logger.info("QQ 网关 READY，连接成功：channel=%s", self.channel_id)
                    elif opcode == 0 and payload.get("t") == "C2C_MESSAGE_CREATE":
                        await self._receive(payload["d"])
            finally:
                self.connected = False
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
        if not self.ready.is_set():
            raise ConnectionError("QQ 网关在 READY 前关闭")
        logger.warning("QQ 网关连接已关闭：%s", self.channel_id)

    async def _receive(self, data: dict[str, Any]) -> None:
        """将好友文本直接提交，网关继续接收后续消息。"""
        target = data.get("author", {}).get("user_openid")
        msg_id = data.get("id")
        text = data.get("content", "").strip()
        if not target or not msg_id or not text:
            logger.info("忽略非文本或不完整的 QQ 私聊消息：%s", self.channel_id)
            return
        request_id = f"{self.channel_id}:{msg_id}"
        # 当前实例携带自己的会话选择，历史和长期记忆仍可与其他入口共用。
        message = InboundMessage(request_id, self.channel_id, target, self.session_id, text, input_type(text))
        if text.startswith("/approve "):
            self.submit(message)
            return
        if request_id in self.replies:
            logger.info("忽略正在处理的重复 QQ 消息：%s", request_id)
            return
        reply = QQReply(target, msg_id)
        self.replies[request_id] = reply
        self.submit(message)

    async def _send_text(self, reply: QQReply, text: str) -> None:
        """通过 SDK 发送被动纯文本回复，递增同一原始消息的发送序号。"""
        if not text:
            return
        reply.seq += 1
        result = await self.api.post_c2c_message(openid=reply.target, msg_type=0, content=text,
                                               msg_id=reply.msg_id, msg_seq=reply.seq)
        if result is None:
            raise RuntimeError("QQ 消息发送未返回结果，可能请求超时")

    async def send(self, reply_target: str, event: dict[str, Any]) -> None:
        """把 Runtime 事件转为私聊文本，工具过程不推送，助手回答在 done 时发送。"""
        reply = self.replies[event["request_id"]]
        if reply.target != reply_target:
            raise ValueError("QQ 回复目标与原始消息不匹配")
        kind = event["type"]
        if kind == "session_created":
            # 首次创建只保存 ID，不发送 QQ 提示或回显用户消息。
            self.session_id = event["session_id"]
        elif kind == "items":
            for item in event["items"]:
                if item.get("role") == "assistant" and item.get("type") == "message":
                    for part in item.get("content", []):
                        text = part.get("text") if part.get("type") == "output_text" else part.get("refusal")
                        if text:
                            reply.texts.append(text)
        elif kind == "session":
            self.session_id = event["session_id"]
            await self._send_text(reply, f"{event['title']}：{event['session_id']}")
        elif kind == "sessions":
            entries = event["sessions"]
            text = "\n".join(f"{entry['id']}  {entry['preview']}" for entry in entries) or "暂无历史会话。"
            await self._send_text(reply, text + "\n恢复会话：/resume <会话ID>")
        elif kind == "approval":
            await self._send_text(reply, f"工具 {event['name']} 需要确认：{event['reason']}\n"
                                  f"参数：{event['arguments']}\n"
                                  f"允许：/approve {event['approval_id']} allow\n"
                                  f"拒绝：/approve {event['approval_id']} deny")
        elif kind in {"status", "error"}:
            await self._send_text(reply, event["text"])
        elif kind == "done":
            try:
                await self._send_text(reply, "\n\n".join(reply.texts))
            finally:
                self.replies.pop(event["request_id"], None)

    async def stop(self) -> None:
        """取消并等待网关与心跳、提交断线通知、关闭 SDK HTTP 连接。"""
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await self.task
            self.task = None
        self.connected = False
        for target in {reply.target for reply in self.replies.values()}:
            self.submit(ChannelDisconnected(self.channel_id, target))
        self.replies.clear()
        if self.http is not None:
            await self.http.close()
            self.http = None
