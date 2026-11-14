import asyncio
import json
import os
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx
from openai import AsyncOpenAI
from websockets.asyncio.client import connect

from src.agent import core, hook
from src.channels.manager import ChannelManager
from src.runtime.runner import AgentRunner
from src.runtime.runtime import Runtime
from src.session.session_store import SessionStore, omit_none
from src.terminal import TerminalClient
from src.tools import tools
from tests.temp_workspace import temporary_workspace
from tests.unit.test_sessions import assistant_message, function_call


class ScriptedUI:
    """替代键盘交互，仍通过真实 TerminalClient 和 WebSocket 访问服务。"""

    def __init__(self, inputs: list[str]) -> None:
        """保存脚本输入、收到的历史快照与实时消息。"""
        self.inputs = iter(inputs)
        self.snapshots: list[list[dict]] = []
        self.items: list[dict] = []
        self.allow = True

    async def read_input(self) -> str:
        """按顺序输入命令和问题。"""
        return next(self.inputs)

    def show_session(self, history: list[dict], title: str) -> None:
        """记录客户端实际收到的会话历史。"""
        self.snapshots.append(history)

    def show_items(self, items: list[dict]) -> None:
        """记录客户端实际收到的原始消息项。"""
        self.items.extend(items)

    async def confirm_permission(self, event: dict) -> bool:
        """为需要审批的测试返回明确决定。"""
        return self.allow


class ChannelTests(unittest.IsolatedAsyncioTestCase):
    """验证客户端、真实 WebSocket、队列、Agent 和 JSONL 的完整本地链路。"""

    async def asyncSetUp(self) -> None:
        """创建独立服务和真实 SDK 模拟传输，失败也关闭连接并删除文件。"""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = self.stack.enter_context(temporary_workspace("miniagent-channel-"))
        self.outputs: list[list[dict]] = []
        self.requests: list[dict] = []
        self.stack.enter_context(patch.dict(os.environ, {"MODEL": "channel-test-model"}))
        self.stack.enter_context(patch.object(tools, "WORKDIR", self.root))
        self.stack.enter_context(patch.object(hook, "WORKDIR", self.root))
        self.stack.enter_context(patch.object(core, "load_memories", new=AsyncMock(return_value="")))
        self.stack.enter_context(patch.object(core, "build_system_prompt", return_value="按要求调用工具。"))
        self.client = AsyncOpenAI(api_key="local-test", base_url="https://channel-test.invalid/v1",
                                  max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.respond)))
        self.addAsyncCleanup(self.client.close)
        runner = AgentRunner(self.client)
        self.stack.enter_context(patch.object(runner, "extract", new=AsyncMock(return_value=0)))
        self.runtime = Runtime(runner, self.root / "sessions")
        self.manager = ChannelManager(self.runtime.queue, self.runtime.approvals, self.runtime.dispatcher)
        # 先关闭监听、再排空消费者，最后关闭 SDK 和删除临时目录。
        self.addAsyncCleanup(self.close_service)
        await self.runtime.start()
        await self.manager.start({"channels": [{"id": "terminal_test", "type": "terminal",
                                                 "options": {"host": "127.0.0.1", "port": 0}}]})
        self.url = self.runtime.dispatcher.channels["terminal_test"].uri

    def respond(self, request: httpx.Request) -> httpx.Response:
        """让真实 SDK 解析预设 Responses 响应，意外额外请求直接失败。"""
        self.requests.append(json.loads(request.content))
        output = self.outputs.pop(0)
        return httpx.Response(200, json={
            "id": f"resp_{len(self.requests)}", "object": "response", "created_at": 1,
            "model": "channel-test-model", "status": "completed", "output": output,
            "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        })

    async def close_service(self) -> None:
        """无论断言是否通过都回收监听器、连接、消费者和后台任务。"""
        await self.manager.stop()
        await self.runtime.stop()

    async def exchange(self, socket, text: str, session_id: str = "") -> list[dict]:
        """从真实协议收集一次请求的完整事件。"""
        await socket.send(json.dumps({"input": text, "session_id": session_id}))
        events = []
        while True:
            event = json.loads(await asyncio.wait_for(socket.recv(), 10))
            events.append(event)
            if event["type"] == "done":
                return events

    async def test_terminal_new_tools_and_reconnect(self) -> None:
        """终端 /new 后执行读取、glob 和计划工具，断开重连并继续同一历史。"""
        (self.root / "note.txt").write_text("口令：青松42", encoding="utf-8")
        self.outputs.extend([[
            function_call("read", "read_file", '{"path":"note.txt"}'),
            function_call("glob", "glob", '{"pattern":"*.txt"}'),
            function_call("plan", "update_plan", '{"plan":[{"content":"读取口令","status":"completed"}]}'),
        ], [assistant_message("口令：青松42")], [assistant_message("仍然是青松42")]])
        original_read = tools.run_read

        def verify_recorded_before_read(path: str) -> str:
            """真实工具开始前，整批调用必须已经持久化。"""
            files = list((self.root / "sessions").rglob("*.jsonl"))
            store, _ = SessionStore.load(files[0])
            self.assertEqual(set(store.pending_calls), {"read", "glob", "plan"})
            return original_read(path)

        ui = ScriptedUI(["/new", "读取文件并更新计划", "/q"])
        terminal = TerminalClient(self.url, ui)
        with patch.dict(core.TOOL_HANDLERS, {"read_file": verify_recorded_before_read}):
            await asyncio.wait_for(terminal.run(), 15)
        self.assertEqual(len(ui.snapshots), 2)
        self.assertTrue(all(not history for history in ui.snapshots))
        self.assertEqual([item["call_id"] for item in ui.items if item["type"] == "function_call_output"],
                         ["read", "glob", "plan"])
        path = next((self.root / "sessions").rglob("*.jsonl"))
        prefix = path.read_bytes()
        async with connect(self.url, proxy=None) as socket:
            events = await self.exchange(socket, "/attach", terminal.session_id)
            snapshot = next(event for event in events if event["type"] == "session")
            self.assertIn("青松42", json.dumps(snapshot, ensure_ascii=False))
            await self.exchange(socket, "继续回忆口令", terminal.session_id)
        store, history = SessionStore.load(path)
        self.assertTrue(path.read_bytes().startswith(prefix))
        self.assertEqual(store.turn_id, 1)
        self.assertFalse(store.pending_calls)
        self.assertEqual(omit_none(self.requests[-1]["input"][:-1]), history[:-2])
        self.assertFalse(any(item.get("content") == "/new" for item in history))

    async def test_new_does_not_create_empty_files(self) -> None:
        """仅连接、新建和断开，不应留下空 JSONL 文件。"""
        ui = ScriptedUI(["/new", "/q"])
        await TerminalClient(self.url, ui).run()
        self.assertFalse(list((self.root / "sessions").rglob("*.jsonl")))
        self.assertFalse(self.requests)

    async def test_permission_allow_and_deny(self) -> None:
        """审批绕过消费者返回决定，允许时执行一次，拒绝时不执行。"""
        self.outputs.extend([
            [function_call("allow", "powershell", '{"command":"Set-Content fixture approved"}')],
            [assistant_message("已批准")],
            [function_call("deny", "powershell", '{"command":"Set-Content fixture denied"}')],
            [assistant_message("已拒绝")],
        ])
        executed = []

        def command(command: str) -> str:
            """记录工具调用，测试审批链路而不更改真实 PowerShell 环境。"""
            executed.append(command)
            return "执行完成"

        ui = ScriptedUI([])
        terminal = TerminalClient(self.url, ui)
        async with connect(self.url, proxy=None) as socket:
            terminal.connection = socket
            await terminal.request("/attach")
            with patch.dict(core.TOOL_HANDLERS, {"powershell": command}):
                await asyncio.wait_for(terminal.request("需要批准"), 10)
                ui.allow = False
                await asyncio.wait_for(terminal.request("需要拒绝"), 10)
        self.assertEqual(executed, ["Set-Content fixture approved"])
        outputs = [item["output"] for item in ui.items if item["type"] == "function_call_output"]
        self.assertIn("权限被拒绝", outputs[-1])

    async def test_session_commands_and_shared_history(self) -> None:
        """两个连接共用历史，恢复和删除命令通过服务端处理。"""
        self.outputs.extend([[assistant_message("第一会话")], [assistant_message("第二会话")]])
        async with connect(self.url, proxy=None) as first, connect(self.url, proxy=None) as second:
            events = await self.exchange(first, "/attach")
            session_id = events[-1]["session_id"]
            await self.exchange(first, "第一条", session_id)
            snapshot = await self.exchange(second, "/resume " + session_id)
            self.assertEqual(next(e for e in snapshot if e["type"] == "session")["items"][0]["content"], "第一条")
            created = await self.exchange(second, "/new", session_id)
            other = created[-1]["session_id"]
            await self.exchange(second, "第二条", other)
            await self.exchange(first, "/resume " + session_id, session_id)
            async with connect(self.url, proxy=None) as reconnected:
                attached = await self.exchange(reconnected, "/attach")
                self.assertEqual(attached[-1]["session_id"], session_id)
            listing = await self.exchange(first, "/resume", session_id)
            self.assertEqual(len(next(e for e in listing if e["type"] == "sessions")["sessions"]), 2)
            deleted = await self.exchange(second, f"/delete {other} confirm", other)
            self.assertFalse(any(e["type"] == "error" for e in deleted))
            self.assertFalse(any(path.stem == other for path in (self.root / "sessions").rglob("*.jsonl")))

    async def test_persistence_failure_stops_tool_execution(self) -> None:
        """调用记录落盘失败时关闭生成器，工具不能产生副作用。"""
        self.outputs.extend([[function_call("blocked", "get_current_time", "{}")]])
        original = SessionStore.append

        def fail_call(store, item, model_name=None):
            """只模拟工具调用记录写入失败，用户输入仍正常保存。"""
            if getattr(item, "type", None) == "function_call":
                raise OSError("模拟磁盘写入失败")
            return original(store, item, model_name)

        executed = []
        async with connect(self.url, proxy=None) as socket:
            attached = await self.exchange(socket, "/attach")
            with patch.object(SessionStore, "append", fail_call), patch.dict(
                    core.TOOL_HANDLERS, {"get_current_time": lambda: executed.append(True)}):
                events = await self.exchange(socket, "时间", attached[-1]["session_id"])
        self.assertFalse(executed)
        self.assertTrue(any(e["type"] == "error" and "磁盘" in e["text"] for e in events))
        self.assertTrue(all(not state.lock.locked() for state in self.runtime.sessions.values()))

    async def test_disconnect_keeps_accepted_request_running(self) -> None:
        """客户端退出不取消已接受请求，完成后其他连接能读取结果。"""
        self.outputs.extend([[assistant_message("断线后仍保存")]])
        started = asyncio.Event()
        release = asyncio.Event()
        original = self.client.responses.create

        async def delayed_response(**kwargs):
            """在模型请求中模拟客户端断线，再允许响应完成。"""
            started.set()
            await release.wait()
            return await original(**kwargs)

        with patch.object(self.client.responses, "create", side_effect=delayed_response):
            async with connect(self.url, proxy=None) as socket:
                events = await self.exchange(socket, "/attach")
                session_id = events[-1]["session_id"]
                await socket.send(json.dumps({"input": "断线测试", "session_id": session_id}))
                await asyncio.wait_for(started.wait(), 5)
            release.set()
            await asyncio.wait_for(self.runtime.queue.join(), 5)
        async with connect(self.url, proxy=None) as socket:
            events = await self.exchange(socket, "/resume " + session_id)
        snapshot = next(event for event in events if event["type"] == "session")
        self.assertIn("断线后仍保存", json.dumps(snapshot, ensure_ascii=False))

    async def test_restart_restores_latest_session(self) -> None:
        """常驻协调层重建后从 JSONL 恢复，不依赖前一次进程的内存。"""
        self.outputs.extend([[assistant_message("已保存历史")], [assistant_message("恢复后继续")]])
        async with connect(self.url, proxy=None) as socket:
            events = await self.exchange(socket, "/attach")
            session_id = events[-1]["session_id"]
            await self.exchange(socket, "重启前的问题", session_id)
        await self.close_service()
        self.runtime = Runtime(AgentRunner(self.client), self.root / "sessions")
        self.stack.enter_context(patch.object(self.runtime.runner, "extract", new=AsyncMock(return_value=0)))
        self.manager = ChannelManager(self.runtime.queue, self.runtime.approvals, self.runtime.dispatcher)
        await self.runtime.start()
        await self.manager.start({"channels": [{"id": "terminal_test", "type": "terminal",
                                                 "options": {"host": "127.0.0.1", "port": 0}}]})
        self.url = self.runtime.dispatcher.channels["terminal_test"].uri
        async with connect(self.url, proxy=None) as socket:
            events = await self.exchange(socket, "/attach")
            self.assertEqual(events[-1]["session_id"], session_id)
            await self.exchange(socket, "重启后的问题", session_id)
        self.assertEqual(self.requests[-1]["input"][0]["content"], "重启前的问题")


if __name__ == "__main__":
    unittest.main()
