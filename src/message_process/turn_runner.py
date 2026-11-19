import asyncio
import json
import logging
from copy import deepcopy
from typing import Any

from openai import AsyncOpenAI

from src.channels.base import InboundMessage, MessageDispatcher
from src.config import Config
from src.context_compact.context_compactor import ContextCompactor, is_context_length_error
from src.hook.hook import check_approval_required, check_powershell_deny_command, trigger_hooks
from src.hook.permissions import ApprovalBroker
from src.memory.memory import extract_memories, load_memories
from src.session.session import Session
from src.session.session_manager import SessionManager, serializable
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
        approvals: ApprovalBroker,
        background_tasks: list[asyncio.Task],
    ) -> None:
        """使用共享的模型、工具、会话和后台任务列表，以及消息处理器的审批对象。"""
        self.client = client
        self.config = config
        self.tool_manager = tool_manager
        self.session_manager = session_manager
        self.message_dispatcher = message_dispatcher
        self.approvals = approvals
        self.background_tasks = background_tasks

    async def process_event(self, inbound_message: InboundMessage, session: Session, event: dict[str, Any]) -> None:
        """原始事件先落盘再分发，审批事件在原对象中填写决定。"""
        if event["type"] == "items":
            for item in event["items"]:
                self.session_manager.append(session, item, model_name=event.get("model_name"))
            await self.message_dispatcher.send(inbound_message, {"type": "items", "items": [serializable(item) for item in event["items"]]})
        elif event["type"] == "permission":
            event["allowed"] = await self.approvals.request(inbound_message, event)
        else:
            await self.message_dispatcher.send(inbound_message, event)

    # 核心
    async def run_turn(self, inbound_message: InboundMessage) -> None:
        """用户输入已落盘，由调用方取得会话占用后循环请求模型、执行工具并处理事件。"""
        session = self.session_manager.get(inbound_message.session_id)
        try:
            messages = session.messages
            current_request = inbound_message.input
            # 每次调用对应一个用户 turn，计划仅在本轮工具循环内共用，不跨 turn 保留。
            # 当前通道未提供工作目录，在 turn 入口使用配置中的目录。
            tool_context = ToolContext(plan=Plan(), current_working_directory=self.config.workdir)
            # 进入工具循环前只召回一次相关记忆，本轮共用同一份动态系统提示词。
            relevant_memories = await load_memories(self.client, messages, self.config)
            instructions = build_system_prompt(self.config, relevant_memories)
            # ToolManager 已在启动阶段注册工具，核心循环不处理初始化任务。
            available_tools = self.tool_manager.get_schemas()
            # 当前任务使用同一个压缩器，负责请求前压缩和超限后的单次补救。
            compactor = ContextCompactor(self.client, self.config)
            # 记录当前模型请求已进行的响应式压缩重试次数。
            reactive_retries = 0
            # 固定本轮使用的模型名称，也用于持久化元信息。
            model_name = self.config.model
            while True:
                # 上下文压缩
                messages[:] = await compactor.prepare(messages, current_request)
                # 发送当前对话，请求模型继续推理或调用工具。
                try:
                    response = await self.client.responses.create(
                        model=model_name, instructions=instructions, input=messages, tools=available_tools
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
                await self.process_event(inbound_message, session, {
                    "type": "items", "items": response.output, "model_name": model_name,
                })
                messages.extend(response.output)

                # 从模型本轮输出中筛选所有函数工具调用。
                tool_calls = [item for item in response.output if item.type == "function_call"]

                # 没有工具调用时，模型已经给出最终答案，本次 Agent 循环结束。
                if not tool_calls:
                    extra_input = trigger_hooks("Stop", messages)
                    # todo: 后续优化点，不简单以不调用工具为结束标志
                    if extra_input is not None:
                        # Harness 的续跑要求属于系统提示，不冒充真实用户输入。
                        extra_message = {"role": "system", "content": extra_input}
                        await self.process_event(inbound_message, session, {"type": "items", "items": [extra_message]})
                        messages.append(extra_message)
                        continue
                    break

                # 依次分发模型请求的工具调用，并收集 OpenAI 格式的字符串结果。
                results = []
                for tool_call in tool_calls:
                    try:
                        # 这里是因为claude返回就是python字典，而openAI是json，要转换下
                        arguments = json.loads(tool_call.arguments)
                        if not isinstance(arguments, dict):
                            raise TypeError("工具参数必须是 JSON 对象")

                        # 工具执行前触发 Hook，返回非 None 值时跳过本次工具调用。
                        blocked = trigger_hooks("PreToolUse", tool_call, arguments)
                        # 闸门 1：高危规则直接拒绝；闸门 2 和 3：敏感操作由来源客户端审批。
                        denied = check_powershell_deny_command(str(arguments.get("command", ""))) if tool_call.name == "powershell" else None
                        reason = check_approval_required(tool_call.name, arguments, self.config) if denied is None else None
                        if blocked is None:
                            if reason:
                                permission = {
                                    "type": "permission", "name": tool_call.name, "arguments": arguments,
                                    "reason": reason, "allowed": False,
                                }
                                # 处理事件时填写审批结果，再决定是否执行；审批异常由轮次恢复处理。
                                await self.process_event(inbound_message, session, permission)
                                if not permission["allowed"]:
                                    denied = "权限被拒绝。"
                            if denied:
                                logger.warning("工具 %s 被权限策略拒绝：%s", tool_call.name, denied)
                                output = denied
                            else:
                                # 统一分发本地与 MCP 工具；当前模型输出协议仍使用字符串结果。
                                output = str(await self.tool_manager.call(tool_call.name, arguments, tool_context))
                                trigger_hooks("PostToolUse", tool_call, output)
                    except (json.JSONDecodeError, TypeError) as error:
                        logger.warning("工具 %s 参数无效：%s", tool_call.name, error)
                        output = f"工具参数无效：{error}"

                    result = {"type": "function_call_output", "call_id": tool_call.call_id, "output": output}
                    await self.process_event(inbound_message, session, {"type": "items", "items": [result]})
                    results.append(result)

                # 整批工具结果之后追加 Hook 可能返回的系统消息。
                extra_input = trigger_hooks("PostToolBatch", tool_context)
                if extra_input:
                    extra_message = {"role": "system", "content": extra_input}
                    await self.process_event(inbound_message, session, {"type": "items", "items": [extra_message]})
                    results.append(extra_message)
                messages.extend(results)
        except BaseException:
            # 轮次中断后以内存外的原始记录重建历史，下轮再补齐缺失工具结果。
            self.session_manager.reload(session)
            raise
        # Agent 正常结束后，提取本轮快照，不受后续追加和压缩影响。
        self.background_tasks.append(asyncio.create_task(
            extract_memories(self.client, deepcopy(session.messages), self.config)))
