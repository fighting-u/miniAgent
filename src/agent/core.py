import asyncio
import json
import logging
import os
from copy import deepcopy
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI

from src.context.context_compactor import ContextCompactor, is_context_length_error
from src.agent.hook import trigger_hooks
from src.common.logging_config import setup_logging
from src.memory.memory import extract_memories, load_memories
from src.tools.mcp_client import MCPClient
from src.session.session_store import SessionStore, activate_session
from src.agent.system_prompt import build_system_prompt
from src.ui.terminal_ui import COMMANDS, TerminalUI
from src.tools.tools import PLAN, TOOL_HANDLERS, TOOLS, initialize_tools


# 当前模块使用的日志记录器。
logger = logging.getLogger(__name__)

# 连续多少轮未更新计划后，向模型追加一次规划提醒。
PLAN_REMINDER_INTERVAL = 3

# 单次模型请求因上下文过长而进行响应式压缩的最大重试次数。
MAX_REACTIVE_COMPACT_RETRIES = 1


async def agent_loop(
    client: AsyncOpenAI, messages: list[Any], active_request: str, session: SessionStore,
    background_tasks: list[asyncio.Task[int]],
    mcp: MCPClient | None = None, tools_ready: asyncio.Task | None = None,
) -> None:
    """持续请求模型并执行工具，直到模型返回最终文本。"""
    # 进入工具循环前只召回一次相关记忆，本轮共用同一份动态系统提示词。
    relevant_memories = await load_memories(client, messages)
    instructions = build_system_prompt(relevant_memories)
    # 辅助召回无需等待 MCP，主模型请求前必须取得完整工具列表。
    available_tools = await tools_ready if tools_ready is not None else TOOLS
    # 记录连续多少轮工具调用没有使用计划工具。
    rounds_since_plan = 0
    # 当前任务使用同一个压缩器，负责请求前压缩和超限后的单次补救。
    compactor = ContextCompactor(client, os.getenv("MODEL", ""))
    # 记录当前模型请求已进行的响应式压缩重试次数。
    reactive_retries = 0
    # 固定本轮使用的模型名称，也用于持久化元信息。
    model_name = os.getenv("MODEL", "")
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
        for item in response.output:
            session.append(item, model_name=model_name)
        messages.extend(response.output)
        # 按响应原始顺序展示推理、工具调用和回答，工具执行前即可看到调用内容。
        TerminalUI.show_items(response.output)

        # 从模型本轮输出中筛选所有函数工具调用。
        tool_calls = [item for item in response.output if item.type == "function_call"]

        # 没有工具调用时，模型已经给出最终答案，本次 Agent 循环结束。
        if not tool_calls:
            forced_input = trigger_hooks("Stop", messages)
            # todo: 后续优化点，不简单以不调用工具为结束标志
            if forced_input is not None:
                # Harness 的续跑要求属于系统提示，不冒充真实用户输入。
                forced_message = {"role": "system", "content": forced_input}
                session.append(forced_message)
                messages.append(forced_message)
                TerminalUI.show_items([forced_message])
                continue
            # 已使用后台任务执行，并由 main 在退出前统一等待。
            # Agent 确定结束本轮循环后，调用辅助模型提取并存储长期记忆。
            # 提取本轮结束时的快照，不受后续追加消息和上下文压缩影响。
            background_tasks.append(asyncio.create_task(extract_memories(client, deepcopy(messages))))
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
                    session.append(result)
                    results.append(result)
                    TerminalUI.show_items([result])
                    continue

                if tool_call.name == "compact":
                    output = "已收到主动压缩请求，将在本轮工具调用全部结束后压缩上下文。"
                    compact_requested = True
                    logger.info("模型主动请求压缩上下文")
                # todo: 是否能统一调用呢？就算不能统一调用，这部分确实也该写成个统一的execute_tool()，不该加重主函数
                elif mcp is not None and tool_call.name in mcp.routes:
                    output = await mcp.call_tool(tool_call.name, arguments)
                else:
                    handler = TOOL_HANDLERS.get(tool_call.name)
                    output = str(handler(**arguments)) if handler else f"未知工具: {tool_call.name}"
                trigger_hooks("PostToolUse", tool_call, output)

                if tool_call.name == "update_plan":
                    used_plan = True
            except (json.JSONDecodeError, TypeError) as error:
                output = f"工具参数无效：{error}"

            result = {"type": "function_call_output", "call_id": tool_call.call_id, "output": output}
            session.append(result)
            results.append(result)
            TerminalUI.show_items([result])

        # 连续多轮未更新计划时，把当前计划和更新提醒一并追加给模型。
        rounds_since_plan = 0 if used_plan else rounds_since_plan + 1
        if rounds_since_plan >= PLAN_REMINDER_INTERVAL:
            reminder = {
                "role": "system",
                "content": f"<reminder>请先检查并使用 update_plan 更新任务计划。\n当前计划：\n{PLAN.render()}</reminder>",
            }
            session.append(reminder)
            results.append(reminder)
            TerminalUI.show_items([reminder])
            rounds_since_plan = 0
        messages.extend(results)
        # 等同普通工具一样先保留调用和结果配对，再整体归档并生成摘要。
        if compact_requested:
            messages[:] = await compactor.compact_history(messages, active_request)

async def main() -> None:
    """自动恢复最近会话，通过终端斜杠命令切换会话或开始提问。"""
    load_dotenv()
    log_path = setup_logging()
    logger.info("MiniAgent 启动，本次日志文件：%s", log_path)
    base_url = os.getenv("BASE_URL")
    # api_key = os.getenv("OPENAI_API_KEY")
    # qwen
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("未找到 API_KEY，请先在 .env 中配置。")

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    # 启动即连接 MCP，与终端输入和辅助模型召回并行。
    mcp = MCPClient()
    tools_ready = asyncio.create_task(initialize_tools(mcp))

    # 保留本进程创建的后台记忆任务，退出前显式等待并收集异常。
    background_tasks: list[asyncio.Task[int]] = []
    try:
        ui = TerminalUI(os.getenv("MODEL", ""))
        sessions = SessionStore.list_sessions()
        title = "新会话"
        startup_error = None
        if sessions:
            try:
                session, history = SessionStore.load(sessions[0]["path"])
                title = "已恢复上次会话"
            except (OSError, ValueError) as error:
                startup_error = str(error)
                session, history = SessionStore.create(), []
        else:
            session = SessionStore.create()
            history = []
        activate_session(session, history, ui, title)
        if startup_error:
            print(f"上次会话无法加载：{startup_error}\n已开启新会话，原文件未修改；可输入 /resume 选择其他会话。")

        while True:
            # 在读取本轮用户输入前触发 Hook，便于扩展输入提示前的行为。
            trigger_hooks("PreUserPromptSubmit")
            try:
                user_request = await ui.read_input()
            except (EOFError, KeyboardInterrupt):
                print("\n结束对话，下次启动将自动恢复最近会话。")
                break
            if not user_request:
                continue
            if user_request == "/q":
                print("结束对话。")
                break
            if user_request == "/new":
                session, history = SessionStore.create(), []
                activate_session(session, history, ui, "新会话")
                continue
            if user_request == "/resume":
                selected = await ui.choose_session(SessionStore.list_sessions())
                if selected is not None:
                    try:
                        restored, restored_history = SessionStore.load(selected)
                    except (OSError, ValueError) as error:
                        print(f"无法恢复该会话：{error}")
                        continue
                    activate_session(restored, restored_history, ui, "已恢复会话")
                    session, history = restored, restored_history
                continue
            if user_request == "/delete":
                available_sessions = SessionStore.list_sessions()
                selected = await ui.choose_session(available_sessions, action="删除")
                if selected is None:
                    continue
                selected_info = next(item for item in available_sessions if item["path"] == selected)
                is_current = selected == session.path
                if not await ui.confirm_delete(selected_info, is_current):
                    print("已取消删除。")
                    continue
                try:
                    SessionStore.delete(selected)
                except (OSError, ValueError) as error:
                    print(f"删除失败：{error}")
                    continue
                logger.info("已删除会话文件：%s", selected)
                if is_current:
                    session, history = SessionStore.create(), []
                    activate_session(session, history, ui, "当前会话已删除，已开启新会话")
                else:
                    print("已删除所选会话文件，此操作无法通过程序恢复。")
                continue
            if user_request.startswith("/"):
                print("未知命令，可用命令：" + "、".join(COMMANDS))
                continue

            history.append(session.start_turn(user_request))
            try:
                # todo：传那么多参数，是否会太乱以及不易维护？
                await agent_loop(client, history, user_request, session=session, background_tasks=background_tasks,
                                 mcp=mcp, tools_ready=tools_ready)
            except KeyboardInterrupt:
                print("\n本轮已中断，下次启动将自动恢复最近会话。")
                break

            # 模型文本和工具交互已在循环中逐步展示，不重复打印最后的回答。
    except asyncio.CancelledError:
        # asyncio.run 在收到 Ctrl+C 时可能取消主任务，仍先完成后台任务收尾。
        print("\n本轮已中断，下次启动将自动恢复最近会话。")
    finally:
        try:
            if background_tasks:
                if any(not task.done() for task in background_tasks):
                    print("正在等待后台记忆提取完成……")
                results = await asyncio.gather(*background_tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        logger.warning("后台记忆提取任务未正常完成：%s", result)
        finally:
            # 所有后台请求结束后再关闭共享客户端，随后 main 返回并关闭事件循环。
            try:
                await mcp.close(tools_ready)
            finally:
                await client.close()

