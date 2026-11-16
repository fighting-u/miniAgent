import asyncio
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from websockets.asyncio.server import serve

from src.channels.base import ChannelDisconnected, InboundMessage, MessageDispatcher
from src.channels.qq import QQChannel
from src.runtime.permissions import ApprovalBroker
from src.config import Config
from src.runtime import runtime as runtime_module
from src.runtime.runtime import AgentRuntime
from tests.temp_workspace import temporary_workspace


async def reply_agent(client, messages, request, config, mcp, plan):
    """用固定回答替换 Agent，验证通道与运行时的事件往返。"""
    item = {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": f"收到：{request}"}]}
    yield {"type": "items", "items": [item]}
    messages.append(item)


class QQTests(unittest.IsolatedAsyncioTestCase):
    """通过本地真实 WebSocket 验证 QQ 协议、消息关联和资源生命周期。"""

    async def asyncSetUp(self) -> None:
        """建立模拟 QQ 网关；不读取凭据、不连接外网、不创建 SDK 日志文件。"""
        self.message_dispatcher = MessageDispatcher()
        self.broker = ApprovalBroker(self.message_dispatcher)
        self.inputs = []  # 记录通道提交的普通输入。
        self.received = asyncio.Event()  # 等待模拟网关实际提交消息。
        self.channel = QQChannel("qq_test", self.submit, startup_timeout=1)
        self.identify = None
        self.heartbeat_sequences = []  # 确认首次心跳使用 READY 的序号，而非鉴权前的空序号。
        self.socket = None
        self.http = SimpleNamespace(close=AsyncMock(), check_session=AsyncMock(),
                                    _token=SimpleNamespace(get_string=Mock(return_value="QQBot local-token")))
        self.api = SimpleNamespace(get_ws_url=AsyncMock(), post_c2c_message=AsyncMock(return_value={"id": "reply"}))
        self.server = await serve(self.gateway, "127.0.0.1", 0)
        self.api.get_ws_url.return_value = {
            "url": f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}", "shards": 1,
        }
        self.login_patch = patch.object(self.channel, "_login", side_effect=self.login)
        self.login_patch.start()
        self.addCleanup(self.login_patch.stop)
        self.addAsyncCleanup(self.close)

    def submit(self, event: InboundMessage | ChannelDisconnected) -> None:
        """模拟 Runtime 提交回调，审批立即分发，其余输入供断言使用。"""
        if isinstance(event, ChannelDisconnected):
            self.broker.disconnect(event.channel_id, event.reply_target)
        elif event.input.startswith("/approve "):
            self.broker.resolve(event)
        else:
            self.inputs.append(event)
        self.received.set()

    async def login(self) -> None:
        """替换鉴权网络，保留真实网关读写与心跳实现。"""
        self.channel.http = self.http
        self.channel.api = self.api

    async def close(self) -> None:
        """无论断言结果如何，关闭通道、WebSocket 监听和全部连接。"""
        await self.channel.stop()
        self.server.close()
        await self.server.wait_closed()

    async def gateway(self, socket) -> None:
        """模拟 Hello、Identify、READY 和心跳确认，保留客户端实际发送的订阅位。"""
        self.socket = socket
        await socket.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 1000}}))
        async for raw in socket:
            payload = json.loads(raw)
            if payload["op"] == 2:
                self.identify = payload["d"]
                await socket.send(json.dumps({"op": 0, "t": "READY", "s": 1, "d": {}}))
            elif payload["op"] == 1:
                self.heartbeat_sequences.append(payload["d"])
                await socket.send(json.dumps({"op": 11, "d": None}))

    async def start(self) -> None:
        """完成握手与心跳后把通道注册到正式 MessageDispatcher。"""
        await self.channel.start()
        await asyncio.wait_for(self.channel.heartbeat_ack.wait(), 2)
        self.message_dispatcher.channels[self.channel.channel_id] = self.channel

    async def receive(self, msg_id: str, text: str) -> None:
        """直接注入平台私聊载荷，验证统一提交入口。"""
        self.received.clear()
        await self.channel._receive({"id": msg_id, "content": text, "author": {"user_openid": "user-a"}})
        await asyncio.wait_for(self.received.wait(), 2)

    async def test_gateway_and_final_reply(self) -> None:
        """验证真正握手、忽略群消息、私聊输入与最终回复的原始 msg_id 关联。"""
        await self.start()
        self.assertTrue(self.channel.connected)
        self.assertEqual(self.identify["intents"], 1 << 25)
        self.assertEqual(self.identify["token"], "QQBot local-token")
        self.assertEqual(self.heartbeat_sequences[0], 1)
        await self.socket.send(json.dumps({"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "s": 2,
                                           "d": {"id": "group-message", "content": "忽略群聊"}}))
        await self.socket.send(json.dumps({"op": 0, "t": "C2C_MESSAGE_CREATE", "s": 3,
                                           "d": {"id": "private-1", "content": "你好",
                                                 "author": {"user_openid": "user-a"}}}))
        await asyncio.wait_for(self.received.wait(), 2)
        message = self.inputs.pop(0)
        self.assertEqual((message.reply_target, message.input, message.session_id), ("user-a", "你好", ""))
        self.assertFalse(self.inputs)
        await self.message_dispatcher.send(message, {"type": "items", "items": [
            {"type": "message", "role": "user", "content": "你好"},
            {"type": "function_call", "name": "read_file"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "回答"}]},
        ]})
        self.api.post_c2c_message.assert_not_awaited()
        await self.message_dispatcher.send(message, {"type": "done"})
        self.api.post_c2c_message.assert_awaited_once_with(openid="user-a", msg_type=0,
                                                         content="回答", msg_id="private-1", msg_seq=1)
        self.assertFalse(self.channel.replies)
        await self.channel.stop()
        self.assertFalse(self.channel.connected)
        self.assertIsNone(self.channel.task)
        self.http.close.assert_awaited_once()

    async def test_approval_bypasses_requests(self) -> None:
        """普通输入等待时审批仍可唤醒执行请求，回复序号保持递增。"""
        await self.start()
        await self.receive("approval-source", "执行工具")
        message = self.inputs.pop(0)
        future = asyncio.create_task(self.broker.request(message, {
            "name": "powershell", "arguments": {"command": "Write-Output ok"}, "reason": "需要确认",
        }))
        self.addAsyncCleanup(self.cancel_task, future)
        # 等待审批真正发送，而不是依赖固定时间 sleep。
        while not self.api.post_c2c_message.await_count:
            await asyncio.sleep(0)
        await self.receive("queued-1", "等待一")
        await self.receive("queued-2", "等待二")
        approval_id = next(iter(self.broker.pending))
        await self.receive("approval-answer", f"/approve {approval_id} allow")
        self.assertTrue(await asyncio.wait_for(future, 1))
        self.assertEqual(len(self.inputs), 2)
        await self.message_dispatcher.send(message, {"type": "status", "text": "已完成"})
        calls = self.api.post_c2c_message.await_args_list
        self.assertEqual([call.kwargs["msg_seq"] for call in calls], [1, 2])
        self.assertTrue(all(call.kwargs["msg_id"] == "approval-source" for call in calls))

    async def cancel_task(self, task) -> None:
        """测试失败时也取消等待中的审批，避免遗留后台任务。"""
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def runtime_receive(self, msg_id: str, text: str) -> None:
        """等待真实 Runtime 处理直接提交的输入并完成该条消息的回复。"""
        await self.channel._receive({"id": msg_id, "content": text, "author": {"user_openid": "user-a"}})

        async def wait_reply() -> None:
            """以消息关联记录释放作为完成条件。"""
            while f"{self.channel.channel_id}:{msg_id}" in self.channel.replies:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_reply(), 2)

    async def test_runtime_new_and_persistence(self) -> None:
        """验证 /new 与私聊通过同一 Runtime 提交入口，只有实际对话创建 JSONL。"""
        with temporary_workspace("miniagent-qq-unit-") as root:
            config = Config(model="local", api_key="local", workdir=root)
            patches = ExitStack()
            # 仅测试临时替换常量，退出时自动恢复。
            patches.enter_context(patch.object(runtime_module, "PROJECT_ROOT", root))
            self.addCleanup(patches.close)
            patches.enter_context(patch.object(runtime_module, "AsyncOpenAI", return_value=SimpleNamespace(close=AsyncMock())))
            patches.enter_context(patch.object(runtime_module, "agent_loop", side_effect=reply_agent))
            patches.enter_context(patch.object(runtime_module, "extract_memories", new=AsyncMock(return_value=0)))
            runtime = AgentRuntime(config)
            runtime.message_dispatcher.channels[self.channel.channel_id] = self.channel
            self.channel.submit = runtime.submit
            self.channel.api = self.api
            await runtime.start()
            try:
                await self.runtime_receive("new-command", "/new")
                self.assertFalse(list(root.rglob("*.jsonl")))
                await self.runtime_receive("chat", "你好")
                records = [json.loads(line) for line in next(root.rglob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
                self.assertEqual([item["payload"]["role"] for item in records], ["user", "assistant"])
                self.assertEqual(self.api.post_c2c_message.await_args.kwargs["content"], "收到：你好")
                self.assertEqual(self.api.post_c2c_message.await_args.kwargs["msg_id"], "chat")
                self.assertFalse(self.channel.replies)
            finally:
                await runtime.stop()

    async def test_start_failure_closes_resources(self) -> None:
        """网关查询失败时不得注册为就绪，并且关闭已创建的 HTTP 连接。"""
        self.api.get_ws_url.side_effect = RuntimeError("模拟鉴权或网关错误")
        self.assertFalse(await self.channel.start())
        self.http.close.assert_awaited_once()
        self.assertIsNone(self.channel.task)

    async def test_start_timeout_cleans_tasks(self) -> None:
        """等不到 READY 时取消网关任务，不留下等待器或 HTTP 连接。"""
        self.api.get_ws_url.side_effect = self.never_gateway
        self.channel.startup_timeout = 0.02
        before = set(asyncio.all_tasks())
        self.assertFalse(await self.channel.start())
        self.assertFalse({task for task in asyncio.all_tasks() - before if not task.done()})
        self.http.close.assert_awaited_once()

    async def never_gateway(self):
        """模拟没有返回结果的网络请求，直到通道取消任务。"""
        await asyncio.Future()


if __name__ == "__main__":
    unittest.main()
