import asyncio
import ctypes
import json
import os
import sys
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dotenv import load_dotenv
from openai import AsyncOpenAI


# 从项目根目录导入实际接入代码，测试不生成 Python 缓存文件。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PROJECT_ROOT))

import core
from context_compactor import ContextCompactor
from mcp_client import MCPClient
from memory import message_text
from session_store import SessionStore
from tools import initialize_tools


# 临时真实 MCP 服务源码：只提供一个工具，执行次数和进程号写入临时目录。
SERVER_SOURCE = '''import os
import sys
from pathlib import Path
from mcp.server.fastmcp import FastMCP

# 服务实例和所有运行痕迹均仅用于本次测试。
server = FastMCP("miniagent-smoke")
root = Path(sys.argv[1])
root.joinpath("server.pid").write_text(str(os.getpid()), encoding="utf-8")

@server.tool()
def reveal_token() -> dict[str, str]:
    """读取本次测试的口令。"""
    with root.joinpath("calls.txt").open("a", encoding="utf-8") as output:
        output.write("called\\n")
    return {"token": root.joinpath("token.txt").read_text(encoding="utf-8")}

if __name__ == "__main__":
    server.run(transport="stdio")
'''


async def run_test(root: Path) -> None:
    """真实启动 MCP 并通过主模型完成一次调用，所有会话写入临时目录。"""
    load_dotenv(PROJECT_ROOT / ".env")
    if not os.getenv("MODEL") or not os.getenv("DASHSCOPE_API_KEY"):
        raise RuntimeError("需要配置 MODEL 和 DASHSCOPE_API_KEY")
    token = "青松-" + uuid.uuid4().hex[:12]
    (root / "token.txt").write_text(token, encoding="utf-8")
    server = root / "server.py"
    server.write_text(SERVER_SOURCE, encoding="utf-8")
    config = root / ".mcp.json"
    config.write_text(json.dumps({"mcpServers": {"smoke": {
        "command": sys.executable, "args": ["-B", str(server), str(root)],
    }}}), encoding="utf-8")
    mcp = MCPClient(config)
    ready = asyncio.create_task(initialize_tools(mcp))
    # 保持进程句柄，用于确认关闭后已退出；失败时也只清理本次创建的进程。
    handle = None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    background = []
    try:
        definitions = await ready
        name = "mcp__smoke__reveal_token"
        assert name in {item["name"] for item in definitions}, "未发现测试工具"
        pid = int((root / "server.pid").read_text(encoding="utf-8"))
        handle = kernel.OpenProcess(0x100001, False, pid)
        assert handle, "无法取得测试进程句柄"
        session = SessionStore.create(root / "sessions")
        question = "请调用测试工具获取口令，然后只回复得到的口令。"
        messages = [session.start_turn(question)]
        async with AsyncOpenAI(api_key=os.getenv("DASHSCOPE_API_KEY"), base_url=os.getenv("BASE_URL"),
                               timeout=60, max_retries=0) as client:
            original_create = client.responses.create
            requests = 0

            async def limited_create(**kwargs):
                """限制两次主模型请求，首次指定唯一 MCP 工具，第二次仅回答。"""
                nonlocal requests
                requests += 1
                if requests > 2:
                    raise AssertionError("超过两次主模型请求")
                kwargs["tool_choice"] = {"type": "function", "name": name} if requests == 1 else "none"
                kwargs["parallel_tool_calls"] = False
                return await original_create(**kwargs)

            async def keep_history(messages, active_request):
                """跳过压缩管线，测试只验证短对话的 MCP 调用。"""
                return messages

            with ExitStack() as patches:
                # 无控制台的测试进程不渲染终端；调用、持久化和结果校验仍走真实逻辑。
                patches.enter_context(patch.object(core.TerminalUI, "show_items"))
                patches.enter_context(patch.object(core, "load_memories", new=AsyncMock(return_value="")))
                patches.enter_context(patch.object(core, "extract_memories", new=AsyncMock(return_value=0)))
                patches.enter_context(patch.object(core, "build_system_prompt", return_value="按用户要求调用工具并简短回答。"))
                patches.enter_context(patch.object(ContextCompactor, "prepare", side_effect=keep_history))
                for method in ("compact_history", "reactive_compact", "summarize_history"):
                    patches.enter_context(patch.object(ContextCompactor, method,
                        new=AsyncMock(side_effect=AssertionError("测试禁止调用压缩或摘要"))))
                patches.enter_context(patch.object(client.responses, "create", side_effect=limited_create))
                await core.agent_loop(client, messages, question, session, background, mcp, ready)
                await asyncio.gather(*background)
            assert requests == 2, "主模型请求次数错误"
        calls = [item for item in messages if getattr(item, "type", None) == "function_call"]
        outputs = [item for item in messages if isinstance(item, dict) and item.get("type") == "function_call_output"]
        assert len(calls) == len(outputs) == 1, "应仅有一次工具调用与结果"
        assert calls[0].name == name and calls[0].call_id == outputs[0]["call_id"], "调用路由或配对错误"
        assert not json.loads(outputs[0]["output"])["isError"], "MCP 返回错误"
        assert token in message_text(messages[-1]), "主模型未正确读取工具结果"
        assert (root / "calls.txt").read_text(encoding="utf-8").splitlines() == ["called"], "服务执行次数不为一次"
        print("真实链路通过：MCP 连接、发现、组装、主 LLM 调用、工具结果配对及最终回答；2 次主模型请求。")
    finally:
        try:
            await asyncio.gather(*background, return_exceptions=True)
            await mcp.close(ready)
        finally:
            if handle:
                try:
                    if kernel.WaitForSingleObject(handle, 5000) != 0:
                        kernel.TerminateProcess(handle, 1)
                        kernel.WaitForSingleObject(handle, 5000)
                        raise AssertionError("MCP 服务未自行退出，已终止测试进程")
                    print("临时 MCP 服务进程已退出。")
                finally:
                    kernel.CloseHandle(handle)


def main() -> None:
    """成功或失败均通过临时目录上下文清理服务源码、配置、口令和会话文件。"""
    root = None
    try:
        with tempfile.TemporaryDirectory(prefix="miniagent-mcp-", dir=PROJECT_ROOT / "tests") as directory:
            root = Path(directory)
            asyncio.run(run_test(root))
    finally:
        if root is not None:
            assert not root.exists(), "临时目录未清理"
            print("临时程序、配置、口令及会话文件已全部清理。")


if __name__ == "__main__":
    main()
