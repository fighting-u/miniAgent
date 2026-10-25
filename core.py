import json
import os
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from hook import trigger_hooks
from system_prompt import SYSTEM
from tools import PLAN, TOOL_HANDLERS, TOOLS


# 系统提示词
INSTRUCTIONS = SYSTEM

# 连续多少轮未更新计划后，向模型追加一次规划提醒。
PLAN_REMINDER_INTERVAL = 3


def agent_loop(client: OpenAI, messages: list[Any]) -> None:
    """持续请求模型并执行工具，直到模型返回最终文本。"""
    # 记录连续多少轮工具调用没有使用计划工具。
    rounds_since_plan = 0
    while True:
        # 发送当前对话，请求模型继续推理或调用工具。
        response = client.responses.create(model=os.getenv("MODEL"), instructions=INSTRUCTIONS, input=messages, tools=TOOLS)
        messages.extend(response.output)

        # 从模型本轮输出中筛选所有函数工具调用。
        tool_calls = [item for item in response.output if item.type == "function_call"]

        # 没有工具调用时，模型已经给出最终答案，本次 Agent 循环结束。
        if not tool_calls:
            forced_input = trigger_hooks("Stop", messages)
            # todo: 后续优化点，不简单以不调用工具为结束标志
            if forced_input is not None:
                messages.append({"role": "user", "content": forced_input})
                continue
            return

        # 依次分发模型请求的工具调用，并收集 OpenAI 格式的字符串结果。
        results = []
        used_plan = False
        for tool_call in tool_calls:
            try:
                # 这里是因为claude返回就是python字典，而openAI是json，要转换下
                arguments = json.loads(tool_call.arguments)

                # 工具执行前触发 Hook，返回非 None 值时跳过本次工具调用。
                blocked = trigger_hooks("PreToolUse", tool_call, arguments)
                if blocked is not None:
                    # todo: 这里给llm的反馈太笼统了，后续可优化细分
                    results.append(
                        {"type": "function_call_output", "call_id": tool_call.call_id, "output": str(blocked)}
                    )
                    continue

                handler = TOOL_HANDLERS.get(tool_call.name)
                output = str(handler(**arguments)) if handler else f"未知工具: {tool_call.name}"
                trigger_hooks("PostToolUse", tool_call, output)

                if tool_call.name == "update_plan":
                    used_plan = True
            except (json.JSONDecodeError, TypeError) as error:
                output = f"工具参数无效：{error}"

            results.append({"type": "function_call_output", "call_id": tool_call.call_id, "output": output})

        # 连续多轮未更新计划时，把当前计划和更新提醒一并追加给模型。
        rounds_since_plan = 0 if used_plan else rounds_since_plan + 1
        if rounds_since_plan >= PLAN_REMINDER_INTERVAL:
            results.append(
                {
                    "role": "user",
                    "content": f"<reminder>请先检查并使用 update_plan 更新任务计划。\n当前计划：\n{PLAN.render()}</reminder>",
                }
            )
            rounds_since_plan = 0
        messages.extend(results)


def main() -> None:
    """加载配置，读取用户运行时输入并启动一次 Agent 任务。"""
    load_dotenv()
    base_url = os.getenv("BASE_URL")
    # api_key = os.getenv("OPENAI_API_KEY")
    # qwen
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("未找到 API_KEY，请先在 .env 中配置。")

    client = OpenAI(api_key=api_key, base_url=base_url)

    print("请输入问题或者任务，enter提交，输入q结束对话。")
    history = []

    while True:
        # 在读取本轮用户输入前触发 Hook，便于扩展输入提示前的行为。
        trigger_hooks("PreUserPromptSubmit")
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
