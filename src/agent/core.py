import asyncio
import json
import logging
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from src.config import Config

from src.context.context_compactor import ContextCompactor, is_context_length_error
from src.agent.hook import check_deny_list, check_rules, trigger_hooks
from src.memory.memory import load_memories
from src.tools.mcp_manager import MCPManager
from src.agent.system_prompt import build_system_prompt
from src.tools.tools import PlanManager, build_tool_handlers, TOOLS


# 当前模块使用的日志记录器。
logger = logging.getLogger(__name__)

# 连续多少轮未更新计划后，向模型追加一次规划提醒。
PLAN_REMINDER_INTERVAL = 5

# 单次模型请求因上下文过长而进行响应式压缩的最大重试次数。
MAX_REACTIVE_COMPACT_RETRIES = 1


async def agent_loop(
    client: AsyncOpenAI, messages: list[Any], active_request: str,
    config: Config,
    mcp: MCPManager | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """持续请求模型和执行工具，通过事件生成器交给外层持久化与分发。"""
    # 每次调用对应一个用户 turn，计划仅在本轮工具循环内共用，不跨 turn 保留。
    plan = PlanManager()
    # 进入工具循环前只召回一次相关记忆，本轮共用同一份动态系统提示词。
    relevant_memories = await load_memories(client, messages, config)
    instructions = build_system_prompt(config, relevant_memories)
    handlers = build_tool_handlers(config, plan)
    # MCPManager 已在启动阶段组装完整工具列表，核心循环不处理初始化任务。
    available_tools = mcp.available_tools if mcp is not None else TOOLS
    # 记录连续多少轮工具调用没有使用计划工具。
    rounds_since_plan = 0
    # 当前任务使用同一个压缩器，负责请求前压缩和超限后的单次补救。
    compactor = ContextCompactor(client, config)
    # 记录当前模型请求已进行的响应式压缩重试次数。
    reactive_retries = 0
    # 固定本轮使用的模型名称，也用于持久化元信息。
    model_name = config.model
    while True:
        # 每次请求模型前依次执行确定性裁剪，必要时再生成历史摘要。
        messages[:] = await compactor.prepare(messages, active_request)
        # 发送当前对话，请求模型继续推理或调用工具。
        try:
            response = await client.responses.create(model=model_name, instructions=instructions, input=messages, tools=available_tools)
            # print(response.output)
            reactive_retries = 0
        except Exception as error:
            if is_context_length_error(error) and reactive_retries < MAX_REACTIVE_COMPACT_RETRIES:
                logger.warning("模型请求因上下文过长被拒绝，开始响应式压缩并重试")
                messages[:] = await compactor.reactive_compact(messages, active_request)
                reactive_retries += 1
                continue
            raise
        # 按响应原始顺序产出推理、工具调用和回答，外层完成落盘后才恢复执行工具。
        yield {"type": "items", "items": response.output, "model_name": model_name}
        messages.extend(response.output)

        # 从模型本轮输出中筛选所有函数工具调用。
        tool_calls = [item for item in response.output if item.type == "function_call"]

        # 没有工具调用时，模型已经给出最终答案，本次 Agent 循环结束。
        if not tool_calls:
            forced_input = trigger_hooks("Stop", messages)
            # todo: 后续优化点，不简单以不调用工具为结束标志
            if forced_input is not None:
                # Harness 的续跑要求属于系统提示，不冒充真实用户输入。
                forced_message = {"role": "system", "content": forced_input}
                yield {"type": "items", "items": [forced_message]}
                messages.append(forced_message)
                continue
            return

        # 依次分发模型请求的工具调用，并收集 OpenAI 格式的字符串结果。
        results = []
        used_plan = False
        compact_requested = False
        for tool_call in tool_calls:
            try:
                # 这里是因为claude返回就是python字典，而openAI是json，要转换下
                arguments = json.loads(tool_call.arguments)

                # 工具执行前触发 Hook，返回非 None 值时跳过本次工具调用。
                blocked = trigger_hooks("PreToolUse", tool_call, arguments)
                if blocked is not None:
                    # todo: 这里给llm的反馈太笼统了，后续可优化细分
                    result = {"type": "function_call_output", "call_id": tool_call.call_id, "output": str(blocked)}
                    yield {"type": "items", "items": [result]}
                    results.append(result)
                    continue

                # 闸门 1：高危规则直接拒绝；闸门 2 和 3：敏感操作由来源客户端审批。
                denied = check_deny_list(str(arguments.get("command", ""))) if tool_call.name == "powershell" else None
                reason = check_rules(tool_call.name, arguments, config) if denied is None else None
                if reason:
                    permission = {"type": "permission", "name": tool_call.name,
                                  "arguments": arguments, "reason": reason, "allowed": False}
                    # 外层消费事件时填写审批结果，恢复生成器后再决定是否执行。
                    yield permission
                    if not permission["allowed"]:
                        denied = "权限被拒绝。"
                if denied:
                    logger.warning("工具 %s 被权限策略拒绝：%s", tool_call.name, denied)
                    result = {"type": "function_call_output", "call_id": tool_call.call_id, "output": denied}
                    yield {"type": "items", "items": [result]}
                    results.append(result)
                    continue

                if tool_call.name == "compact":
                    output = "已收到主动压缩请求，将在本轮工具调用全部结束后压缩上下文。"
                    compact_requested = True
                    logger.info("模型主动请求压缩上下文")
                # todo: 是否能统一调用呢？就算不能统一调用，这部分确实也该写成个统一的execute_tool()，不该加重主函数
                elif mcp is not None and tool_call.name in mcp.routes:
                    output = await mcp.call_tool(tool_call.name, arguments)
                else:
                    handler = handlers.get(tool_call.name)
                    if tool_call.name == "update_plan":
                        output = str(handler(**arguments))
                    else:
                        # 同步工具移到工作线程，保持常驻端监听和审批收发可响应。
                        output = str(await asyncio.to_thread(handler, **arguments)) if handler else f"未知工具: {tool_call.name}"
                trigger_hooks("PostToolUse", tool_call, output)

                if tool_call.name == "update_plan":
                    used_plan = True
            except (json.JSONDecodeError, TypeError) as error:
                output = f"工具参数无效：{error}"

            result = {"type": "function_call_output", "call_id": tool_call.call_id, "output": output}
            yield {"type": "items", "items": [result]}
            results.append(result)

        # 连续多轮未更新计划时，把当前计划和更新提醒一并追加给模型。
        rounds_since_plan = 0 if used_plan else rounds_since_plan + 1
        if rounds_since_plan >= PLAN_REMINDER_INTERVAL:
            reminder = {
                "role": "system",
                "content": f"系统提醒：当前已连续{PLAN_REMINDER_INTERVAL}轮没使用或更新任务计划，如果任务复杂或步骤多请使用 update_plan 更新任务计划。\n当前计划：\n{plan.render()}",
            }
            yield {"type": "items", "items": [reminder]}
            results.append(reminder)
            rounds_since_plan = 0
        messages.extend(results)
        # 等同普通工具一样先保留调用和结果配对，再整体归档并生成摘要。
        if compact_requested:
            messages[:] = await compactor.compact_history(messages, active_request)

