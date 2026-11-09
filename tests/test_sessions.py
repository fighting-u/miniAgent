import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import httpx
from openai import OpenAI
from openai.types.responses import ResponseFunctionToolCall, ResponseReasoningItem

import core
import tools
from context_compactor import ContextCompactor
from session_store import API_FORMAT, SessionStore, omit_none


def assistant_message(text: str) -> dict:
    """生成供真实 SDK 解析的简短助手消息响应。"""
    return {
        "type": "message",
        "id": "msg_answer",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": [], "logprobs": None}],
    }


def function_call(call_id: str, name: str, arguments: str) -> dict:
    """生成一条 Responses 格式工具调用。"""
    return {
        "type": "function_call",
        "id": f"msg_{call_id}",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "status": "completed",
    }


class SessionTests(unittest.TestCase):
    """验证实际 SDK 序列化、会话落盘以及核心循环的中断恢复。"""

    def setUp(self) -> None:
        """创建自动清理的隔离工作区，禁用辅助记忆调用，并禁止触发任何压缩落盘。"""
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.session = SessionStore.create(self.root / "sessions")
        self.stack.enter_context(patch.dict(os.environ, {"MODEL": "session-test-model"}))
        self.stack.enter_context(patch.object(tools, "WORKDIR", self.root))
        self.stack.enter_context(patch.object(core, "load_memories", return_value=""))
        self.stack.enter_context(patch.object(core, "extract_memories", return_value=0))
        self.stack.enter_context(patch.object(core, "build_system_prompt", return_value="只进行简短测试。"))
        for name in ("write_transcript", "save_output", "summarize_history"):
            self.stack.enter_context(
                patch.object(ContextCompactor, name, side_effect=AssertionError("测试不应触发压缩"))
            )

    def records(self) -> list[dict]:
        """从磁盘逐行解析会话，避免只验证内存中的状态。"""
        return [json.loads(line) for line in self.session.path.read_text(encoding="utf-8").splitlines()]

    def client(self, outputs: list[list[dict]], requests: list[dict]) -> OpenAI:
        """用 HTTP 模拟响应驱动真实 OpenAI SDK，并收集实际发送的 JSON。"""
        queue = iter(outputs)

        def handle(request: httpx.Request) -> httpx.Response:
            """返回预设响应，模型调用次数超出预期时立即失败。"""
            requests.append(json.loads(request.content))
            output = next(queue)
            return httpx.Response(200, json={
                "id": f"resp_{len(requests)}", "object": "response", "created_at": 1,
                "model": "session-test-model", "status": "completed", "output": output,
                "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
                "error": None, "incomplete_details": None, "instructions": None,
                "metadata": {}, "temperature": 1, "top_p": 1,
            })

        client = OpenAI(
            api_key="local-test-key", base_url="https://session-test.invalid/v1",
            max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        self.addCleanup(client.close)
        return client

    def test_path_uuid_and_roundtrip(self) -> None:
        """验证 UUIDv7、日期路径、SDK 别名及四类记录的无损有效内容恢复。"""
        self.assertEqual(UUID(self.session.path.stem).version, 7)
        self.assertEqual(self.session.path.parent.relative_to(self.root / "sessions").as_posix(),
                         datetime.now().astimezone().strftime("%Y/%m/%d"))
        user = self.session.start_turn("你好，保存中文。")
        reasoning = ResponseReasoningItem(id="rs_1", type="reasoning", summary=[])
        call = ResponseFunctionToolCall(
            id="fc_1", type="function_call", call_id="call_1", name="read_file",
            arguments='{"path": "说明.txt", "extra": null}', **{"async": False},
        )
        self.session.append(reasoning, model_name="test-model")
        self.session.append(call, model_name="test-model")
        self.session.append({"type": "function_call_output", "call_id": "call_1", "output": ""})
        self.session.append(assistant_message("已保存。"), model_name="test-model")
        original_bytes = self.session.path.read_bytes()
        restored, history = SessionStore.load(self.session.path)
        self.assertEqual(restored.path, self.session.path)
        self.assertEqual(restored.ordinal, 5)
        self.assertEqual(restored.turn_id, 0)
        self.assertEqual(history[0], user)
        self.assertEqual(history[2]["arguments"], call.arguments)
        self.assertIs(history[2]["async"], False)
        self.assertNotIn("async_", history[2])
        self.assertFalse(restored.pending_calls)
        self.assertTrue(all("metadata" not in item for item in history))
        rows = self.records()
        for row in rows:
            self.assertEqual(set(row), {"timestamp", "ordinal", "type", "payload", "turn_id"})
            self.assertEqual(row["type"], "messages")
            self.assertIsNotNone(datetime.fromisoformat(row["timestamp"]).tzinfo)
        for index in (1, 4):
            self.assertEqual(rows[index]["payload"]["metadata"]["model_name"], "test-model")
        for index in (0, 2, 3):
            self.assertEqual(rows[index]["payload"]["metadata"], {"api_format": API_FORMAT})
        self.assertNotIn("logprobs", rows[4]["payload"]["content"][0])
        self.assertNotIn("encrypted_content", rows[1]["payload"])
        restored.start_turn("继续。")
        self.assertTrue(self.session.path.read_bytes().startswith(original_bytes))
        self.assertEqual(self.records()[-1]["ordinal"], 5)
        self.assertEqual(self.records()[-1]["turn_id"], 1)

    def test_omit_none_keeps_other_empty_values(self) -> None:
        """只省略对象的 None 字段，不吞掉其他值或列表位置。"""
        value = {"null": None, "zero": 0, "false": False, "text": "", "list": [],
                 "object": {}, "nested": [{"null": None, "text": ""}, None]}
        self.assertEqual(omit_none(value), {
            "zero": 0, "false": False, "text": "", "list": [], "object": {},
            "nested": [{"text": ""}, None],
        })
        self.assertIn("null", value)

    def test_core_tools_then_resume(self) -> None:
        """执行真实读取和 glob 工具，再加载字典历史向真实 SDK 继续发请求。"""
        (self.root / "note.txt").write_text("验证口令：青松42", encoding="utf-8")
        reasoning = {"id": "rs_1", "type": "reasoning", "summary": []}
        requests = []
        client = self.client([
            [reasoning, function_call("read_1", "read_file", '{"path":"note.txt"}'),
             function_call("glob_1", "glob", '{"pattern":"*.txt"}')],
            [assistant_message("口令是青松42。")],
            [assistant_message("上次的口令仍是青松42。")],
        ], requests)
        history = [self.session.start_turn("读取文件并查找 txt 文件。")]
        core.agent_loop(client, history, "读取文件并查找 txt 文件。", session=self.session)
        prefix = self.session.path.read_bytes()
        rows = self.records()
        self.assertEqual([row["payload"]["type"] for row in rows], [
            "message", "reasoning", "function_call", "function_call",
            "function_call_output", "function_call_output", "message",
        ])
        self.assertEqual(rows[4]["payload"]["output"], "验证口令：青松42")
        self.assertEqual(rows[5]["payload"]["output"], "note.txt")
        self.assertEqual(rows[2]["payload"]["call_id"], rows[4]["payload"]["call_id"])
        self.assertEqual(rows[3]["payload"]["call_id"], rows[5]["payload"]["call_id"])
        resumed, history = SessionStore.load(self.session.path)
        old_history = deepcopy(history)
        question = "上次的口令是什么？"
        history.append(resumed.start_turn(question))
        core.agent_loop(client, history, question, session=resumed)
        self.assertEqual(requests[-1]["input"][:-1], old_history)
        self.assertNotIn("metadata", json.dumps(requests[-1]["input"]))
        self.assertTrue(self.session.path.read_bytes().startswith(prefix))
        self.assertEqual([row["ordinal"] for row in self.records()], list(range(9)))
        self.assertEqual([row["turn_id"] for row in self.records()], [0] * 7 + [1, 1])
        self.assertFalse(ContextCompactor.is_model_output(history[0]))

    def test_interrupted_tool_batch_can_continue_without_replay(self) -> None:
        """批次中途断开时保留首个结果，只给缺失结果补说明，不重复执行工具。"""
        requests = []
        client = self.client([[
            function_call("done", "read_file", '{"path":"first"}'),
            function_call("pending", "read_file", '{"path":"second"}'),
        ], [assistant_message("已恢复，可以继续。")]], requests)
        executed = []

        def interrupted_read(path: str) -> str:
            """在第二个工具调用时模拟 Ctrl+C。"""
            executed.append(path)
            if path == "second":
                raise KeyboardInterrupt
            return "首个结果"

        history = [self.session.start_turn("读取两个文件。")]
        with patch.dict(core.TOOL_HANDLERS, {"read_file": interrupted_read}):
            with self.assertRaises(KeyboardInterrupt):
                core.agent_loop(client, history, "读取两个文件。", session=self.session)
        self.assertEqual(self.records()[-1]["payload"]["call_id"], "done")
        resumed, history = SessionStore.load(self.session.path)
        self.assertEqual(list(resumed.pending_calls), ["pending"])
        history.extend(resumed.recover_pending_tools())
        self.assertIn("无法确认", history[-1]["output"])
        self.assertEqual(resumed.recover_pending_tools(), [])
        history.append(resumed.start_turn("继续问候即可。"))
        core.agent_loop(client, history, "继续问候即可。", session=resumed)
        self.assertEqual(executed, ["first", "second"])
        SessionStore.load(self.session.path)

    def test_invalid_arguments_and_blocked_results_are_saved(self) -> None:
        """无效参数与权限拦截也必须产生可配对、可恢复的结果记录。"""
        for blocked in (False, True):
            with self.subTest(blocked=blocked):
                self.session = SessionStore.create(self.root / "sessions")
                requests = []
                client = self.client([[
                    function_call("call_1", "read_file", '{}' if blocked else '{bad json'),
                ], [assistant_message("已收到工具反馈。")]], requests)
                history = [self.session.start_turn("简单工具测试。")]

                def hook_result(event: str, *args):
                    """仅在执行前拦截，Stop Hook 仍返回 None，避免意外续跑。"""
                    return "权限被拒绝。" if blocked and event == "PreToolUse" else None

                with patch.object(core, "trigger_hooks", side_effect=hook_result):
                    core.agent_loop(client, history, "简单工具测试。", session=self.session)
                result = self.records()[2]["payload"]
                self.assertEqual(result["call_id"], "call_1")
                self.assertIn("权限被拒绝" if blocked else "工具参数无效", result["output"])
                SessionStore.load(self.session.path)

    def test_cli_new_session_and_resume(self) -> None:
        """通过 main 创建会话，重新启动时自动加载原文件并继续提问。"""
        requests = []
        client = self.client([[assistant_message("你好。")], [assistant_message("欢迎回来。")]], requests)
        with ExitStack() as stack:
            stack.enter_context(patch.object(core, "OpenAI", return_value=client))
            stack.enter_context(patch.object(core, "load_dotenv"))
            stack.enter_context(patch.object(core, "setup_logging", return_value="测试日志"))
            stack.enter_context(patch.object(SessionStore, "create", return_value=self.session))
            session_list = stack.enter_context(patch.object(SessionStore, "list_sessions", return_value=[]))
            ui = stack.enter_context(patch.object(core, "TerminalUI")).return_value
            stack.enter_context(patch.dict(os.environ, {"DASHSCOPE_API_KEY": "local-test-key"}))
            stack.enter_context(redirect_stdout(io.StringIO()))
            ui.read_input.side_effect = ["你好", "/q"]
            core.main()
            prefix = self.session.path.read_bytes()
            session_list.return_value = [{"path": self.session.path}]
            ui.read_input.side_effect = ["继续聊天", "/q"]
            core.main()
        self.assertEqual(len(requests[-1]["input"]), 3)
        self.assertEqual(requests[-1]["input"][0]["content"], "你好")
        self.assertTrue(self.session.path.read_bytes().startswith(prefix))
        self.assertEqual([row["turn_id"] for row in self.records()], [0, 0, 1, 1])

    def test_corrupt_or_unsupported_files_fail_without_rewriting(self) -> None:
        """损坏尾行、错误序号和未实现的 compact 明确拒绝，保留原文件。"""
        self.session.start_turn("你好。")
        original = self.records()[0]
        for kind in ("ordinal", "turn_id", "compact", "partial", "newline", "api_format", "orphan"):
            with self.subTest(kind=kind):
                record = deepcopy(original)
                if kind == "ordinal":
                    record["ordinal"] = 2
                elif kind == "turn_id":
                    record["turn_id"] = 2
                elif kind == "compact":
                    record["type"] = "compact"
                elif kind == "api_format":
                    record["payload"]["metadata"]["api_format"] = "unsupported"
                elif kind == "orphan":
                    record["payload"] = {"type": "function_call_output", "call_id": "missing",
                                         "output": "无对应调用", "metadata": {"api_format": API_FORMAT}}
                text = json.dumps(record, ensure_ascii=False) + "\n"
                if kind == "partial":
                    text += '{"ordinal":'
                elif kind == "newline":
                    text = text.rstrip("\n")
                self.session.path.write_text(text, encoding="utf-8")
                before = self.session.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "会话文件第"):
                    SessionStore.load(self.session.path)
                self.assertEqual(self.session.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
