import asyncio
import logging
from copy import deepcopy
from pathlib import Path

from openai import AsyncOpenAI

from src.channels.base import InboundMessage, MessageDispatcher
from src.common.common import persist_and_dispatch_items
from src.config import Config
from src.hook.hook import trigger_hooks
from src.memory.memory import extract_memories, load_memories
from src.session.session_manager import SessionManager
from src.system_prompt import build_system_prompt
from src.tools.local_tools import Plan
from src.tools.tool import ToolContext
from src.tools.tool_manager import ToolManager

# 记录模型请求、压缩重试和工具执行情况。
logger = logging.getLogger(__name__)

# 单次模型请求因上下文过长而进行响应式压缩的最大重试次数。
MAX_REACTIVE_COMPACT_RETRIES = 1


class TurnRunner:
    """执行一轮已经开始的用户请求，并保存和分发产生的事件。"""

    def __init__(
        self,
        client: AsyncOpenAI,
        config: Config,
        tool_manager: ToolManager,
        session_manager: SessionManager,
        message_dispatcher: MessageDispatcher,
        background_tasks: list[asyncio.Task],
    ) -> None:
        """使用共享的模型、工具、会话、消息分发器和后台任务列表。"""
        self.client = client
        self.config = config
        self.tool_manager = tool_manager
        self.session_manager = session_manager
        self.message_dispatcher = message_dispatcher
        self.background_tasks = background_tasks

    # 核心
    async def run_turn(self, inbound_message: InboundMessage) -> None:
        """用户输入已落盘，由调用方取得会话占用后循环请求模型、处理工具调用并分发消息。"""
        session = self.session_manager.get(inbound_message.session_id)
        try:
            messages = session.messages
            current_request = inbound_message.input
            # 每次调用对应一个用户 turn，计划仅在本轮工具循环内共用，不跨 turn 保留。
            # 当前通道未提供工作目录，在 turn 入口取常驻进程的当前目录。
            tool_context = ToolContext(plan=Plan(), cwd=Path.cwd().resolve())
            # 进入工具循环前只召回一次相关记忆，本轮共用同一份动态系统提示词。
            relevant_memories = await load_memories(self.client, messages, self.config)
            instructions = build_system_prompt(self.config, tool_context.cwd, relevant_memories)
            # ToolManager 已在启动阶段注册工具，核心循环不处理初始化任务。
            available_tools = self.tool_manager.get_schemas()
            # 当前任务使用同一个压缩器，负责请求前压缩和超限后的单次补救。
            compactor = ContextCompactor(self.client, self.config)
            # 记录当前模型请求已进行的响应式压缩重试次数。
            reactive_retries = 0
            # 固定本轮使用的模型名称，也用于持久化元信息。
            model_name = self.config.model
            while True:
                # 发送当前对话，请求模型继续推理或调用工具。
                try:
                    response = await self.client.responses.create(
                        model=model_name, instructions=instructions, input=messages,
                        tools=available_tools, parallel_tool_calls=True,
                    )
                    # print(response.output)
                    reactive_retries = 0
                except Exception as error:
                    if is_context_length_error(error) and reactive_retries < MAX_REACTIVE_COMPACT_RETRIES:
                        logger.warning("模型请求因上下文过长被拒绝，开始响应式压缩并重试")
                        messages[:] = await compactor.reactive_compact(messages, current_request)
                        reactive_retries += 1
                        continue
                    raise
                # 按响应原始顺序落盘并分发推理、工具调用和回答，然后才执行工具。
                await persist_and_dispatch_items(
                    inbound_message, session, response.output, self.session_manager, self.message_dispatcher, model_name=model_name
                )
                messages.extend(response.output)

                # 从模型本轮输出中筛选所有函数工具调用；托管工具已由模型服务端执行。
                tool_calls = [item for item in response.output if item.type == "function_call"]

                # 没有工具调用时，模型已经给出最终答案，本次 Agent 循环结束。
                if not tool_calls:
                    extra_input = trigger_hooks("Stop", messages)
                    # todo: 后续优化点，不简单以不调用工具为结束标志
                    if extra_input is not None:
                        # Harness 的续跑要求属于系统提示，不冒充真实用户输入。
                        extra_message = {"role": "system", "content": extra_input}
                        await persist_and_dispatch_items(inbound_message, session, [extra_message], self.session_manager, self.message_dispatcher)
                        messages.append(extra_message)
                        continue
                    break

                # 工具请求已随响应保存，批内结果由 ToolManager 按原顺序落盘并发送。
                await self.tool_manager.execute_batch(
                    tool_calls, tool_context, session, inbound_message, self.message_dispatcher, self.session_manager
                )
        except BaseException:
            # 轮次中断后以内存外的原始记录重建历史，下轮再补齐缺失工具结果。
            self.session_manager.reload(session)
            raise
        # Agent 正常结束后，提取本轮快照，不受后续追加和压缩影响。
        self.background_tasks.append(asyncio.create_task(extract_memories(self.client, deepcopy(session.messages), self.config)))
