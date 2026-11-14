from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from src.agent.core import agent_loop
from src.memory.memory import extract_memories
from src.tools.mcp_client import MCPClient
from src.tools.tools import PlanManager


class Runner(ABC):
    """执行调度边界，后续执行池仍向协调层提供相同的事件流。"""

    @abstractmethod
    def run(self, messages: list[Any], request: str, plan: PlanManager) -> AsyncIterator[dict[str, Any]]:
        """执行一轮请求；事件落盘成功后才能继续产生工具副作用。"""

    async def extract(self, messages: list[Any]) -> int:
        """预留辅助记忆提取，非模型测试执行器默认无需处理。"""
        return 0


class AgentRunner(Runner):
    """在常驻事件循环内执行 Agent，共享已初始化的模型与 MCP 资源。"""

    def __init__(self, client: AsyncOpenAI, mcp: MCPClient | None = None, tools_ready: Any = None) -> None:
        """保存服务生命周期内可复用的运行资源。"""
        self.client = client
        self.mcp = mcp
        self.tools_ready = tools_ready

    def run(self, messages: list[Any], request: str, plan: PlanManager) -> AsyncIterator[dict[str, Any]]:
        """提供异步生成器，协调层拥有迭代和关闭的控制权。"""
        return agent_loop(self.client, messages, request, self.mcp, self.tools_ready, plan)

    async def extract(self, messages: list[Any]) -> int:
        """使用外层提供的历史快照提取共享长期记忆。"""
        return await extract_memories(self.client, messages)
