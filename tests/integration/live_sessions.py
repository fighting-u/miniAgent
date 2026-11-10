import argparse
import json
import os
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv
from openai import OpenAI


# 脚本可从项目根目录直接启动，复用实际核心循环与工具处理函数。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.agent import core
from src.tools import tools
from src.context.context_compactor import ContextCompactor
from src.memory.memory import message_text
from src.session.session_store import SessionStore


def read_records(path: Path) -> list[dict]:
    """解析实际磁盘记录，供跨进程验证使用。"""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def worker(phase: str, state_path: Path) -> None:
    """在独立进程中调用真实 API，首轮结束后硬退出，第二轮从 JSONL 恢复。"""
    load_dotenv(PROJECT_ROOT / ".env")
    # 与 core.main 使用同一套环境配置，不输出密钥或其他认证内容。
    model = os.getenv("MODEL")
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not model or not api_key:
        raise RuntimeError("真实测试需要配置 MODEL 和 DASHSCOPE_API_KEY。")
    if phase == "first":
        # 会话和工具读取的文件均位于父进程管理的临时目录，不写入正式 sessions。
        session = SessionStore.create(state_path.parent / "sessions")
        history = []
        token = f"青松-{session.path.stem[-8:]}"
        fixture = session.path.with_suffix(".fixture.txt")
        fixture.write_text(f"本次会话的验证口令是：{token}", encoding="utf-8")
        state_path.write_text(json.dumps({"path": str(session.path), "token": token}), encoding="utf-8")
        question = f"请调用 read_file 读取 {fixture.as_posix()}，记住里面的验证口令，只回复该口令。"
    else:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        session, history = SessionStore.load(state["path"])
        token = state["token"]
        if session.pending_calls:
            raise AssertionError("第一进程应已完成工具调用")
        question = "上次读取文件中的验证口令是什么？只回复该口令，无需再次读取。"
    history.append(session.start_turn(question))
    with ExitStack() as stack:
        # 仅保留简短会话和只读文件工具，避免无关记忆写入或压缩增加测试复杂度。
        stack.enter_context(patch.object(tools, "WORKDIR", state_path.parent.resolve()))
        stack.enter_context(patch.object(core, "load_memories", return_value=""))
        stack.enter_context(patch.object(core, "extract_memories", return_value=0))
        stack.enter_context(patch.object(core, "build_system_prompt", return_value=
                                        "你正在进行简短会话测试。按用户要求读取文件、记住口令并简短回答。"))
        allowed_tools = [tool for tool in core.TOOLS if tool["name"] == "read_file"] if phase == "first" else []
        stack.enter_context(patch.object(core, "TOOLS", allowed_tools))
        for name in ("write_transcript", "save_output", "summarize_history"):
            stack.enter_context(patch.object(ContextCompactor, name,
                                            side_effect=AssertionError("真实测试不应触发压缩")))
        client = stack.enter_context(OpenAI(
            api_key=api_key, base_url=os.getenv("BASE_URL"), timeout=60, max_retries=0,
        ))
        original_create = client.responses.create
        call_count = 0

        def limited_create(**kwargs):
            """限制冒烟测试的 API 调用次数，并检查恢复上下文没有附加 metadata。"""
            nonlocal call_count
            call_count += 1
            if call_count > 4:
                raise AssertionError("真实测试超过 4 次 API 调用，停止继续消耗")
            for item in kwargs["input"]:
                if isinstance(item, dict) and "metadata" in item:
                    raise AssertionError("恢复历史仍含持久化 metadata")
            return original_create(**kwargs)

        stack.enter_context(patch.object(client.responses, "create", side_effect=limited_create))
        core.agent_loop(client, history, question, session=session)
    answer = message_text(history[-1])
    if token not in answer:
        raise AssertionError(f"{phase} 阶段未正确回答已读取的口令")
    rows = read_records(session.path)
    if any(row["type"] != "messages" for row in rows):
        raise AssertionError("测试出现非 messages 记录")
    calls = [row for row in rows if row["payload"]["type"] == "function_call"]
    outputs = [row for row in rows if row["payload"]["type"] == "function_call_output"]
    if not calls or any(row["payload"]["name"] != "read_file" for row in calls):
        raise AssertionError("未实际调用只读文件工具")
    if [row["payload"]["call_id"] for row in calls] != [row["payload"]["call_id"] for row in outputs]:
        raise AssertionError("工具调用与结果没有原样配对")
    print(f"{phase} 阶段通过：{len(rows)} 条记录，{call_count} 次真实 API 请求。", flush=True)
    if phase == "first":
        # 不依赖程序正常退出时统一保存，验证每条记录都已经持久化。
        os._exit(0)


def main() -> None:
    """运行两个独立 Python 进程，验证真实 API、进程断开及重新加载续聊。"""
    parser = argparse.ArgumentParser(description="真实会话持久化冒烟测试（使用当前配置的 API）")
    parser.add_argument("--phase", choices=("first", "second"))
    parser.add_argument("--state", type=Path)
    args = parser.parse_args()
    if args.phase:
        if args.state is None:
            parser.error("子进程需要 --state")
        worker(args.phase, args.state)
        return
    # 父进程管理整个临时目录，子进程硬退出或测试失败时也会自动清理本次数据。
    with tempfile.TemporaryDirectory(prefix="miniagent-session-live-") as directory:
        state_path = Path(directory) / "state.json"
        env = dict(os.environ, PYTHONUTF8="1")
        for phase in ("first", "second"):
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "--phase", phase,
                            "--state", str(state_path)], cwd=PROJECT_ROOT, env=env, check=True, timeout=300)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            session_path = Path(state["path"])
            if phase == "first":
                prefix = session_path.read_bytes()
        if not session_path.read_bytes().startswith(prefix):
            raise AssertionError("续聊覆盖了原有记录")
        rows = read_records(session_path)
        if [row["ordinal"] for row in rows] != list(range(len(rows))):
            raise AssertionError("ordinal 不连续")
        if set(row["turn_id"] for row in rows) != {0, 1}:
            raise AssertionError("turn_id 未在恢复后递增")
        restored, _ = SessionStore.load(session_path)
        if restored.pending_calls:
            raise AssertionError("仍有未配对工具调用")
    print("真实跨进程续聊测试通过；本次临时会话、工具读取文件和状态文件已自动清理。")


if __name__ == "__main__":
    main()
