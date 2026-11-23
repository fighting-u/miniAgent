import asyncio
import json
import logging
from inspect import iscoroutinefunction
from typing import Any, Awaitable, Callable

from openai.types.responses import ResponseFunctionToolCall

from src.channels.base import InboundMessage
from src.common.common import to_openai_tool_output
from src.config import Config
from src.hook.hook import HookManager
from src.security.tool_guard import audit_tool_request
from src.session.session import Session
from src.session.session_manager import SessionManager
from src.tools.local_tools import register_local_tools
from src.tools.mcp_manager import MCPManager
from src.tools.tool import Tool, ToolContext, ToolResult, text_result


# 工具注册和调用的日志记录器。
logger = logging.getLogger(__name__)

class ToolManager:
    """管理本地和 MCP 工具的执行，汇总这两类工具和模型托管工具的声明。"""

    def __init__(self, config: Config, session_manager: SessionManager, hook_manager: HookManager) -> None:
        """创建各 turn 共用的工具注册表和 MCP 管理器。"""
        self._config = config
        self.session_manager = session_manager  # 保存工具结果，并通过会话的消息分发器发送审批请求。
        self._hook_manager = hook_manager  # 触发工具执行前后的 Hook。
        self._mcp = MCPManager(config)
        self._tools: dict[str, Tool] = {}

    async def start(self) -> None:
        """注册本地工具，等待 MCP 工具发现完成后统一注册。"""
        register_local_tools(self, self._config)
        for tool in await self._mcp.start():
            self.register(tool)

    async def stop(self) -> None:
        """关闭 MCP 连接并清空工具注册表。"""
        try:
            await self._mcp.stop()
        finally:
            self._tools.clear()

    def register(self, tool: Tool) -> None:
        """注册工具，重名时记录日志并拒绝覆盖。"""
        if tool.name in self._tools:
            logger.error("工具名称重复：%s", tool.name)
            raise ValueError(f"工具名称重复：{tool.name}")
        self._tools[tool.name] = tool

    def register_local_tool(
        self, *, name: str, func: Callable[..., ToolResult | Awaitable[ToolResult]], description: str,
        parameters: dict[str, Any], strict: bool | None = None, uses_context: bool = False,
        supports_parallel: bool = False,
    ) -> None:
        """使用显式 schema 注册本地函数，按需传入本轮状态。"""
        is_async = iscoroutinefunction(func)
        properties = parameters["properties"]

        # 装饰器
        async def execute(arguments: dict[str, Any], context: ToolContext) -> ToolResult:
            """校验模型参数，避免覆盖注册时绑定或由 Agent 传入的内部依赖。"""
            if unexpected := arguments.keys() - properties.keys():
                raise TypeError(f"未声明的工具参数：{', '.join(sorted(unexpected))}")
            if uses_context:
                arguments = {**arguments, "context": context}
            if is_async:
                return await func(**arguments)
            # 同步工具转异步，用新线程，避免阻塞监听等
            worker = asyncio.create_task(asyncio.to_thread(func, **arguments))
            try:
                # 这里是为了以后增加取消/暂停执行的能力，shield保证外层协程被取消时不会直接取消本协程，而是先抛取消异常，等协程执行完
                # 因为加了工具读写锁，避免暂停了，工具还没执行完，锁却释放了，恢复时被后面的工具占了，导致2个本该串行的写并发了
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                # 已启动的线程无法取消，等它退出后再由调用方释放工具锁。
                await asyncio.gather(worker, return_exceptions=True)
                raise

        self.register(Tool(name, description, parameters, execute, strict, supports_parallel=supports_parallel))

    async def execute(
        self, call: ResponseFunctionToolCall, context: ToolContext, session: Session,
        inbound_message: InboundMessage,
    ) -> ToolResult:
        """执行工具"""
        try:
            # 这里是因为claude返回就是python字典，而openAI是json，要转换下
            arguments = json.loads(call.arguments)
            if not isinstance(arguments, dict):
                raise TypeError("工具参数必须是 JSON 对象")
        except (json.JSONDecodeError, TypeError) as error:
            logger.warning("工具 %s 参数无效：%s", call.name, error)
            return text_result(f"工具参数无效：{error}, 请检查工具调用格式", is_error=True)

        # 工具执行前触发 Hook
        self._hook_manager.trigger("PreToolUse", call, arguments)
        # 统一审计工具请求，先检查高危规则，再检查是否需要审批。
        feedback = await audit_tool_request(
            call.name, arguments, self._config, context, session, inbound_message, self.session_manager.message_dispatcher,
        )
        if feedback is not None:
            return text_result(feedback, is_error=True)

        tool = self._tools.get(call.name)
        if tool is None:
            logger.warning("调用了未注册的工具：%s", call.name)
            return text_result(f"工具不存在: {call.name}，请检查工具名称", is_error=True)
        # 获取读写锁。
        lock = session.tool_lock.reader_lock if tool.supports_parallel else session.tool_lock.writer_lock
        async with lock:
            # 不管本地，mcp工具，统一执行入口
            output = await tool.execute(arguments, context)
        self._hook_manager.trigger("PostToolUse", call, output)
        return output

    async def execute_batch(
        self, calls: list[ResponseFunctionToolCall], context: ToolContext, session: Session,
        inbound_message: InboundMessage,
    ) -> None:
        """并发提交已落盘的调用，按模型调用顺序保存、发送结果并更新会话上下文。"""
        tasks = [
            asyncio.create_task(self.execute(call, context, session, inbound_message))
            for call in calls
        ]
        try:
            for call, task in zip(calls, tasks):
                try:
                    output = await task
                except Exception as error:
                    logger.exception("工具 %s 执行失败，call_id=%s", call.name, call.call_id)
                    output = text_result(str(error), is_error=True)
                result = to_openai_tool_output(call.call_id, output)
                preview = output.log_output()
                logger.info("工具 %s，call_id=%s：%s", call.name, call.call_id, preview)
                await self.session_manager.persist_and_dispatch_items(
                    inbound_message, session, [result],
                    is_error=output.is_error, output_preview=preview,
                )
                session.messages.append(result)
        except BaseException:
            # 批次中断时收回尚未完成的任务，再交给轮次恢复逻辑。
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        # 整批工具结果之后，以 developer 消息追加 Hook 返回的提醒。
        extra_input = self._hook_manager.trigger("PostToolBatch", context)
        if extra_input:
            extra_message = {"role": "developer", "content": extra_input}
            await self.session_manager.persist_and_dispatch_items(
                inbound_message, session, [extra_message],
            )
            session.messages.append(extra_message)

    def get_schemas(self) -> list[dict[str, Any]]:
        """汇总 Responses API 所需的函数工具和模型托管工具定义。"""
        schemas = []
        for tool in self._tools.values():
            schema: dict[str, Any] = {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            if tool.strict is not None:
                schema["strict"] = tool.strict
            schemas.append(schema)
        schemas.extend({"type": tool_type} for tool_type in self._config.model_hosted_tools)
        return schemas
