import asyncio
import logging
from inspect import iscoroutinefunction
from typing import Any, Callable

from src.config import Config
from src.tools.local_tools import register_local_tools
from src.tools.mcp_manager import MCPManager
from src.tools.tool import Tool, ToolContext


# 工具注册和调用的日志记录器。
logger = logging.getLogger(__name__)


class ToolManager:
    """管理本地和 MCP 工具的注册与生命周期，提供 schema 和调用入口。"""

    def __init__(self, config: Config) -> None:
        """创建各 turn 共用的工具注册表和 MCP 管理器。"""
        self._config = config
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
        self, *, name: str, func: Callable[..., Any], description: str,
        parameters: dict[str, Any], strict: bool | None = None, uses_context: bool = False,
    ) -> None:
        """使用显式 schema 注册本地函数，按需传入本轮状态。"""
        is_async = iscoroutinefunction(func)
        properties = parameters["properties"]

        async def execute(arguments: dict[str, Any], context: ToolContext) -> Any:
            """校验模型参数，避免覆盖注册时绑定或由 Agent 传入的内部依赖。"""
            if unexpected := arguments.keys() - properties.keys():
                raise TypeError(f"未声明的工具参数：{', '.join(sorted(unexpected))}")
            if uses_context:
                arguments = {**arguments, "context": context}
            if is_async:
                return await func(**arguments)
            # 同步工具移到工作线程，保持常驻端监听和审批收发可响应。
            return await asyncio.to_thread(func, **arguments)

        self.register(Tool(name, description, parameters, execute, strict))

    async def call(self, name: str, arguments: dict[str, Any], context: ToolContext) -> Any:
        """按名称执行工具，传入本轮状态并保留原始返回类型。"""
        tool = self._tools.get(name)
        if tool is None:
            logger.warning("调用了未注册的工具：%s", name)
            return f"未知工具: {name}"
        return await tool.execute(arguments, context)

    def get_schemas(self) -> list[dict[str, Any]]:
        """从已注册工具生成 OpenAI Responses 的 function 工具定义。"""
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
        return schemas
