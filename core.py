import json
import os
from pyexpat.errors import messages
import subprocess
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

# 系统提示词
INSTRUCTIONS = """你是一个运行在 Windows 环境中的智能体助手，帮助用户完成任务。"""

# PowerShell 单次执行的最长时间，单位为秒。
POWERSHELL_TIMEOUT_SECONDS = 120

# 目前唯一的可用工具 PowerShell；parameters 使用 JSON Schema 描述模型可传入的参数。
TOOLS = [
    {
        "type": "function",
        "name": "run_powershell",
        "description": (
            "在当前项目目录中运行一条 PowerShell 命令，并返回退出码、标准输出和错误输出。" "适合查看文件、检查环境以及执行用户明确要求的本地操作。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "需要执行的完整 PowerShell 命令。"}},
            "required": ["command"],
            "additionalProperties": False,
        },
        "strict": True,
    }
]


# 目前唯一的可用工具 PowerShell
def run_powershell(command: str) -> dict[str, Any]:
    """在当前项目目录执行 PowerShell 命令，并返回可序列化的执行结果。"""
    # 用小写关键词拦截常见的递归删除、磁盘格式化以及关机重启命令。
    dangerous_commands = ["remove-item -recurse", "format-volume", "clear-disk", "stop-computer", "restart-computer", "shutdown", "reboot"]
    normalized_command = command.lower()
    if any(item in normalized_command for item in dangerous_commands):
        return {"error": "检测到危险 PowerShell 命令，已阻止执行。"}

    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + command,
            ],
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=POWERSHELL_TIMEOUT_SECONDS,
            check=False,
        )
        return {"exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
    except subprocess.TimeoutExpired as error:
        return {"error": f"PowerShell 执行超过 {POWERSHELL_TIMEOUT_SECONDS} 秒，已终止。", "stdout": error.stdout or "", "stderr": error.stderr or ""}
    except OSError as error:
        return {"error": f"无法启动 PowerShell：{error}"}


def agent_loop(client: OpenAI, messages: list[Any]) -> None:
    """持续请求模型并执行工具，直到模型返回最终文本。"""
    while True:
        # 发送当前对话，请求模型继续推理或调用工具。
        response = client.responses.create(model=os.getenv("MODEL"), instructions=INSTRUCTIONS, input=messages, tools=TOOLS)
        messages.extend(response.output)

        # 从模型本轮输出中筛选所有 PowerShell 工具调用。
        tool_calls = [item for item in response.output if item.type == "function_call"]

        # 没有工具调用时，模型已经给出最终答案，本次 Agent 循环结束。
        if not tool_calls:
            return

        # 依次执行模型请求的 PowerShell 命令，并收集 OpenAI 格式的工具结果。
        results = []
        for tool_call in tool_calls:
            try:
                # 这里是因为claude返回就是python字典，而openAI是json，要转换下
                arguments = json.loads(tool_call.arguments)
                result = run_powershell(arguments["command"])
            except (json.JSONDecodeError, KeyError, TypeError) as error:
                result = {"error": f"工具参数无效：{error}"}

            results.append({"type": "function_call_output", "call_id": tool_call.call_id, "output": json.dumps(result, ensure_ascii=False)})
        messages.extend(results)


def main() -> None:
    """加载配置，读取用户运行时输入并启动一次 Agent 任务。"""
    load_dotenv()
    base_url = os.getenv("BASE_URL")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("未找到 OPENAI_API_KEY，请先在 .env 中配置。")

    client = OpenAI(api_key=api_key, base_url=base_url)

    print("请输入问题或者任务，enter提交，输入q结束对话。")
    history = []
    
    while True:
        user_request = input("请输入：").strip()
        if user_request.lower() == "q":
            print("结束对话。")
            break
        
        history.append({"role": "user", "content": user_request})
        agent_loop(client, history)

        # 打印模型最后一次返回的文本内容。
        response_content = history[-1].content
        if isinstance(response_content, list):
            for block in response_content:
                if getattr(block, "type", None) == "output_text":
                    print(block.text)
        print()


if __name__ == "__main__":
    main()
