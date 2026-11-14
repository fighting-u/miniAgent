import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch


# 脚本和子进程都不生成缓存，临时文件统一由父进程管理。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PROJECT_ROOT))

from tests.temp_workspace import temporary_workspace


async def server_worker(root: Path) -> None:
    """复用真实常驻启动入口和模型 API，只隔离辅助记忆、配置及测试数据。"""
    from openai import AsyncOpenAI

    from src import server
    from src.agent import core, hook
    from src.channels.manager import ChannelManager
    from src.common import logging_config
    from src.context.context_compactor import ContextCompactor
    from src.runtime.runner import AgentRunner
    from src.tools import tools
    from src.tools.mcp_client import MCPClient

    original_start = ChannelManager.start
    calls = 0

    def create_mcp():
        """本测试只验证内置工具，避免连接用户配置中的外部 MCP 服务。"""
        config = root / "mcp.json"
        config.write_text('{"mcpServers": {}}', encoding="utf-8")
        return MCPClient(config)

    async def publish_ready(manager, settings):
        """在真实监听就绪后发布随机端口，父进程不抢占固定端口。"""
        await original_start(manager, settings)
        uri = next(iter(manager.dispatcher.channels.values())).uri
        (root / "ready.json").write_text(json.dumps({"url": uri}), encoding="utf-8")

    def create_client(**kwargs):
        """使用现有认证配置，限制真实测试的请求超时和重试次数。"""
        client = AsyncOpenAI(**kwargs, timeout=60, max_retries=0)
        original_create = client.responses.create

        async def limited_create(**request):
            """分两轮验证读取与 PowerShell，再以一轮历史续聊完成五次请求。"""
            nonlocal calls
            calls += 1
            if calls > 5:
                raise AssertionError("真实多通道测试超过五次模型请求")
            choices = {1: "read_file", 3: "powershell"}
            request["tool_choice"] = {"type": "function", "name": choices[calls]} if calls in choices else "none"
            request["parallel_tool_calls"] = False
            response = await original_create(**request)
            (root / "model_calls.json").write_text(json.dumps({"count": calls}), encoding="utf-8")
            return response

        client.responses.create = limited_create
        return client

    with ExitStack() as patches:
        patches.enter_context(patch.object(ChannelManager, "start", publish_ready))
        patches.enter_context(patch.object(server, "AsyncOpenAI", side_effect=create_client))
        patches.enter_context(patch.object(server, "MCPClient", side_effect=create_mcp))
        patches.enter_context(patch.object(logging_config, "LOG_DIR", root / "logs"))
        patches.enter_context(patch.object(core, "load_memories", new=AsyncMock(return_value="")))
        patches.enter_context(patch.object(AgentRunner, "extract", new=AsyncMock(return_value=0)))
        patches.enter_context(patch.object(core, "build_system_prompt", return_value=
            "你在验证 Windows 终端工具链。先读取用户指定的文件，再执行用户指定的 Write-Output 命令。"
            "最后回答读取到的口令。后续问题只根据历史回答，不需要计划或额外工具。"))
        patches.enter_context(patch.object(tools, "WORKDIR", root))
        patches.enter_context(patch.object(hook, "WORKDIR", root))
        for name in ("write_transcript", "save_output", "summarize_history"):
            patches.enter_context(patch.object(ContextCompactor, name,
                side_effect=AssertionError("短对话测试不得产生压缩文件")))
        task = asyncio.create_task(server.serve(root / "channels.yaml", root / "sessions"))
        try:
            while not (root / "stop").exists():
                if task.done():
                    await task
                await asyncio.sleep(0.1)
        finally:
            task.cancel()
            results = await asyncio.gather(task, return_exceptions=True)
            if isinstance(results[0], BaseException) and not isinstance(results[0], asyncio.CancelledError):
                raise results[0]


async def terminal_worker(root: Path, phase: str) -> None:
    """另起终端客户端，真实运行 prompt-toolkit 输入与菜单，再通过网络对话。"""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from src.terminal import TerminalClient
    from src.ui.terminal_ui import TerminalUI

    uri = json.loads((root / "ready.json").read_text(encoding="utf-8"))["url"]
    question = f"请调用 read_file 读取 {root.joinpath('note.txt').as_posix()}，记住并回答文件中的口令。"
    command = "请调用 powershell 执行 Write-Output '终端工具正常'，返回命令输出。"
    inputs = iter(["/new", question, command, "/q"] if phase == "first"
                  else ["/resume", "上次文件中的口令是什么？", "/q"])
    received = []
    snapshots = []

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        class AutomatedUI(TerminalUI):
            """只投喂按键，沿用实际输入框、会话菜单和消息渲染。"""

            async def read_input(self) -> str:
                """每轮准备好后输入一行，验证真实输入框返回内容。"""
                text = next(inputs)
                pipe.send_text(text + "\r")
                actual = await super().read_input()
                assert actual == text, f"输入框返回内容不符：{actual!r}"
                return actual

            async def choose_session(self, sessions, action="恢复"):
                """实际操作会话菜单，按序号选择刚创建的会话。"""
                assert len(sessions) == 1, "应仅有本次测试创建的会话"
                pipe.send_text("1\r")
                return await super().choose_session(sessions, action)

            def show_session(self, history, title):
                """保留真实历史渲染，同时记录网络快照以供父进程断言。"""
                snapshots.append(history)
                super().show_session(history, title)

            def show_items(self, items):
                """保留真实角色和工具展示，收集原始项目验证分发结果。"""
                received.extend(items)
                super().show_items(items)

        ui = AutomatedUI("多通道真实测试")
        terminal = TerminalClient(uri, ui)
        await asyncio.wait_for(terminal.run(), 240)
    (root / f"{phase}.json").write_text(json.dumps({
        "session_id": terminal.session_id, "items": received, "snapshots": snapshots,
    }, ensure_ascii=False), encoding="utf-8")


def run_parent() -> None:
    """管理真实常驻端和客户端子进程，成功或失败都关闭进程并删除产物。"""
    root_path = None
    try:
        with temporary_workspace("miniagent-channels-live-") as root:
            root_path = root
            token = "青松-" + root.name[-10:]
            (root / "note.txt").write_text("本次验证口令：" + token, encoding="utf-8")
            (root / "channels.yaml").write_text(json.dumps({"channels": [{
                "id": "terminal_live", "type": "terminal", "options": {"host": "127.0.0.1", "port": 0},
            }]}), encoding="utf-8")
            env = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
            command = [sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()), "--root", str(root)]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            with (root / "server.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(command + ["--role", "server"], cwd=PROJECT_ROOT,
                    env=env, stdout=log, stderr=log, creationflags=creationflags)
            try:
                deadline = time.monotonic() + 30
                while not (root / "ready.json").exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise AssertionError("常驻端未正常启动：" + (root / "server.log").read_text(encoding="utf-8"))
                    time.sleep(0.1)
                prefix = b""
                for phase in ("first", "second"):
                    with (root / f"terminal-{phase}.log").open("w", encoding="utf-8") as log:
                        result = subprocess.run(command + ["--role", "terminal", "--phase", phase],
                            cwd=PROJECT_ROOT, env=env, stdout=log, stderr=log, timeout=260,
                            creationflags=creationflags)
                    if result.returncode:
                        raise AssertionError("终端客户端失败：" + (root / f"terminal-{phase}.log").read_text(encoding="utf-8"))
                    state = json.loads((root / f"{phase}.json").read_text(encoding="utf-8"))
                    assert token in json.dumps(state["items"], ensure_ascii=False), "终端未显示正确口令"
                    errors = [item for item in state["items"] if item.get("type") == "error"]
                    assert not errors, str(errors)
                    paths = list((root / "sessions").rglob("*.jsonl"))
                    assert len(paths) == 1, "/new 或初次连接产生了多余空文件"
                    path = paths[0]
                    if phase == "first":
                        prefix = path.read_bytes()
                        first_id = state["session_id"]
                        calls = [item for item in state["items"] if item["type"] == "function_call"]
                        outputs = [item for item in state["items"] if item["type"] == "function_call_output"]
                        assert [item["name"] for item in calls] == ["read_file", "powershell"], "真实工具未执行"
                        assert [item["call_id"] for item in calls] == [item["call_id"] for item in outputs]
                        assert token in outputs[0]["output"] and "终端工具正常" in outputs[1]["output"]
                    else:
                        assert state["session_id"] == first_id, "恢复到不同会话"
                        assert path.read_bytes().startswith(prefix), "续聊覆盖了旧历史"
                    assert process.poll() is None, "终端断开导致常驻端退出"
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                assert [row["ordinal"] for row in rows] == list(range(len(rows)))
                assert set(row["turn_id"] for row in rows) == {0, 1, 2}
                assert not any(row["payload"].get("content", "").startswith("/")
                               for row in rows if isinstance(row["payload"].get("content"), str))
                assert json.loads((root / "model_calls.json").read_text(encoding="utf-8"))["count"] == 5
                print("真实测试通过：独立终端 /new、read_file、PowerShell、断开重连、/resume 和历史续聊；5 次模型请求。")
            except BaseException:
                print((root / "server.log").read_text(encoding="utf-8"))
                for log_file in (root / "logs").glob("*.log"):
                    print(log_file.read_text(encoding="utf-8")[-10000:])
                raise
            finally:
                (root / "stop").touch()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                    raise AssertionError("常驻端未按时关闭，已终止本次测试进程")
                if process.returncode:
                    raise AssertionError("常驻端关闭失败：" + (root / "server.log").read_text(encoding="utf-8"))
    finally:
        if root_path is not None:
            assert not root_path.exists(), "测试临时目录未清理"
            print("本次测试子进程和临时配置、会话、工具文件、日志已全部清理。")


def main() -> None:
    """区分父进程、真实常驻端和独立终端客户端。"""
    parser = argparse.ArgumentParser(description="多通道真实端到端测试")
    parser.add_argument("--role", choices=("server", "terminal"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--phase", choices=("first", "second"), default="first")
    args = parser.parse_args()
    if args.role == "server":
        asyncio.run(server_worker(args.root))
    elif args.role == "terminal":
        asyncio.run(terminal_worker(args.root, args.phase))
    else:
        run_parent()


if __name__ == "__main__":
    main()
