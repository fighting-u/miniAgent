import asyncio
import logging
from copy import deepcopy
from functools import partial
from pathlib import Path

from openai import AsyncOpenAI

from src.channels.base import InboundMessage
from src.compact.compactor import ContextCompactor
from src.config import Config
from src.hook.hook import trigger_hooks
from src.memory.memory import extract_memories, load_memories, memory_messages
from src.session.session_manager import SessionManager
from src.system_prompt import build_system_prompt
from src.tools.local_tools import Plan
from src.tools.tool import ToolContext
from src.tools.tool_manager import ToolManager

# 记录模型请求、上下文压缩和工具执行情况。
logger = logging.getLogger(__name__)


class TurnRunner:
    """执行一轮用户请求，协调轮前/轮内自动压缩、模型调用和工具执行。"""

    def __init__(
        self,
        client: AsyncOpenAI,
        config: Config,
        tool_manager: ToolManager,
        session_manager: SessionManager,
        compactor: ContextCompactor,
        background_tasks: list[asyncio.Task],
    ) -> None:
        """使用共享的模型、工具、会话管理器、压缩器和后台任务列表。"""
        self.client = client
        self.config = config
        self.tool_manager = tool_manager
        self.session_manager = session_manager
        self.compactor = compactor
        self.background_tasks = background_tasks

    # 核心
    async def run_turn(self, inbound_message: InboundMessage) -> None:
        """由调用方取得会话占用后，先完成轮前恢复与压缩检测，再落盘输入并循环执行本轮。"""
        session = self.session_manager.get(inbound_message.session_id)
        on_status = partial(self.session_manager.message_dispatcher.send_status, inbound_message)
        try:
            # 每次调用对应一个用户 turn，计划仅在本轮工具循环内共用，不跨 turn 保留。
            # 当前通道未提供工作目录，在 turn 入口取常驻进程的当前目录。
            tool_context = ToolContext(plan=Plan(), cwd=Path.cwd().resolve())
            # ToolManager 已在启动阶段注册工具，核心循环不处理初始化任务。
            available_tools = self.tool_manager.get_schemas()
            # 轮前先补齐上次中断的工具调用，再检测上一轮留下的上下文是否需要自动压缩。
            self.session_manager.recover_pending_tools(session)
            # turn前压缩
            await self.compactor.maybe_compact(
                session,
                phase="pre_turn",
                instructions_provider=lambda: build_system_prompt(self.config, tool_context.cwd),
                tools=available_tools,
                on_status=on_status,
            )
            # 在压缩后才加入用户输入，避免用户消息被压缩掉
            self.session_manager.append_user_message(session, inbound_message.input)

            messages = session.messages
            # 进入工具循环前只召回一次相关记忆，本轮共用同一份动态系统提示词。
            relevant_memories = await load_memories(self.client, memory_messages(session), self.config)
            instructions = build_system_prompt(self.config, tool_context.cwd, relevant_memories)
            # 固定本轮使用的模型名称，也用于持久化元信息；优先采用当前会话绑定的 model_info。
            model_name = session.model_info.name if session.model_info is not None else self.config.model
            while True:
                # 发送当前对话，请求模型继续推理或调用工具。
                response = await self.client.responses.create(
                    model=model_name, instructions=instructions, input=messages, tools=available_tools, parallel_tool_calls=True
                )
                # 按响应原始顺序落盘并分发推理、工具调用和回答，然后才执行工具。
                await self.session_manager.persist_and_dispatch_items(
                    inbound_message, session, response.output, model_name=model_name
                )
                messages.extend(response.output)
                # 在全部输出追加后、工具结果追加前记录主响应实际用量，建立窗口估算基准。
                self.session_manager.record_usage(session, "main", response.usage)

                # 从模型本轮输出中筛选所有函数工具调用；托管工具已由模型服务端执行。
                tool_calls = [item for item in response.output if item.type == "function_call"]

                # 没有工具调用时，模型已经给出最终答案，检查 Stop Hook 是否要求续跑。
                if tool_calls:
                    # 工具请求已随响应保存，批内结果由 ToolManager 按原顺序落盘并发送。
                    await self.tool_manager.execute_batch(tool_calls, tool_context, session, inbound_message)
                else:
                    extra_input = trigger_hooks("Stop", messages)
                    # todo: 后续优化点，不简单以不调用工具为结束标志
                    if extra_input is None:
                        break

                    # Harness 的续跑要求属于系统提示，不冒充真实用户输入。
                    extra_message = {"role": "system", "content": extra_input}
                    await self.session_manager.persist_and_dispatch_items(inbound_message, session, [extra_message])
                    messages.append(extra_message)

                # 本轮需要下一次普通模型请求前，统一检测上下文是否达到自动压缩阈值。
                await self.compactor.maybe_compact(
                    session, phase="mid_turn", instructions_provider=lambda: instructions, tools=available_tools, on_status=on_status
                )
        except BaseException:
            # todo: 正常请求超出上下文时，该ui提示
            # 轮次中断后以内存外的原始记录重建历史，下轮再补齐缺失工具结果。
            self.session_manager.reload(session)
            raise
        # Agent 正常结束后，提取本轮快照，不受后续追加和压缩影响。
        self.background_tasks.append(asyncio.create_task(extract_memories(self.client, deepcopy(memory_messages(session)), self.config)))
