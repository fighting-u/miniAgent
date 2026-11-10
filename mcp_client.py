import asyncio
import json
import logging
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


# MCP 连接与工具发现的总等待时间，以及单次工具调用的等待时间。
MCP_CONNECT_TIMEOUT = 30
MCP_CALL_TIMEOUT = 60
# 默认配置位于项目根目录；缺失时仅使用内置工具。
MCP_CONFIG_PATH = Path(__file__).resolve().parent / ".mcp.json"
# 当前模块诊断日志记录器。
logger = logging.getLogger(__name__)


class MCPClient:
    """维护 stdio 服务器连接、静态工具定义及调用路由。"""

    def __init__(self, config_path: Path = MCP_CONFIG_PATH) -> None:
        """保存配置路径和连接任务，所有状态仅属于当前进程。"""
        self.config_path = config_path
        self.tools: list[dict[str, Any]] = []
        self.routes: dict[str, tuple[ClientSession, str]] = {}
        self.tasks: list[asyncio.Task[None]] = []

    async def initialize(self) -> None:
        """并行连接配置中的服务器，全部发现完成后发布静态列表。"""
        try:
            if not self.config_path.exists():
                return
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
            servers = config["mcpServers"]
            if not isinstance(servers, dict):
                raise ValueError("mcpServers 必须是对象")
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"MCP 配置无效，继续使用内置工具：{error}")
            return
        # 每个连接任务自行进入和退出 SDK 上下文，避免跨任务关闭 AnyIO 取消作用域。
        pending = []
        for name, settings in servers.items():
            ready = asyncio.get_running_loop().create_future()
            self.tasks.append(asyncio.create_task(self._serve(name, settings, ready)))
            pending.append(ready)
        discovered = await asyncio.gather(*pending)
        for definitions, routes in discovered:
            if self.routes.keys() & routes.keys():
                # 服务器 a__b 的工具 c，与服务器 a 的工具 b__c，都会得到 mcp__a__b__c；太奇葩了，应该很少出现
                print("MCP 工具名称与其他服务器冲突，跳过该服务器的工具。")
                continue
            self.tools.extend(definitions)
            self.routes.update(routes)

    async def _serve(self, name: str, settings: Any, ready: asyncio.Future) -> None:
        """建立单个连接、发现全部工具，并保持上下文直到收到退出信号。"""
        try:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
                raise ValueError("服务器名称仅支持英文字母、数字、下划线和短横线")
            if not isinstance(settings, dict):
                raise ValueError("服务器配置必须是对象")
            params = StdioServerParameters(**settings)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=MCP_CALL_TIMEOUT)) as session:
                    definitions = []
                    routes = {}
                    with anyio.fail_after(MCP_CONNECT_TIMEOUT):
                        await session.initialize()
                        cursor = None
                        seen_cursors = set()
                        while True:
                            page = await session.list_tools(cursor=cursor)
                            for tool in page.tools:
                                exposed = f"mcp__{name}__{tool.name}"
                                if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", exposed) or exposed in routes:
                                    raise ValueError(f"工具名称无效或重复：{exposed}")
                                definitions.append({
                                    "type": "function", "name": exposed,
                                    "description": tool.description or f"调用 {name} 的 {tool.name} 工具",
                                    "parameters": tool.inputSchema, "strict": False,
                                })
                                routes[exposed] = (session, tool.name)
                            cursor = page.nextCursor
                            if cursor is None:
                                break
                            if cursor in seen_cursors:
                                raise ValueError("工具列表分页游标重复")
                            seen_cursors.add(cursor)
                    if not ready.done():
                        ready.set_result((definitions, routes))
                    # 保持连接，直到 close 取消当前任务并触发上下文退出。
                    await asyncio.Future()
        except Exception as error:
            logger.warning("MCP 服务器 %s 连接或运行失败：%s", name, error)
            print(f"MCP 服务器 {name} 不可用：{error}")
        finally:
            if not ready.done():
                ready.set_result(([], {}))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """调用已注册工具，将文本、结构化结果和错误标志转换为字符串。"""
        try:
            session, original = self.routes[name]
            result = await session.call_tool(original, arguments)
            content = [
                block.model_dump(mode="json", by_alias=True, exclude_none=True)
                if block.type == "text" else {"type": block.type, "text": "暂不支持此类型的 MCP 内容"}
                for block in result.content
            ]
            return json.dumps({"content": content, "structuredContent": result.structuredContent,
                               "isError": result.isError}, ensure_ascii=False)
        except Exception as error:
            return json.dumps({"isError": True, "error": f"MCP 调用失败：{error}"}, ensure_ascii=False)

    async def close(self, initialization: asyncio.Task | None = None) -> None:
        """统一取消初始化及连接任务，等待 SDK 上下文退出并关闭子进程。"""
        # todo:这里有必要等待吗，直接关了也行？
        # cancel 仅请求停止；仍需 gather 等待上下文清理实际完成。
        tasks = [*self.tasks] if initialization is None else [initialization, *self.tasks]
        # 同时终止仍在握手或发现工具的任务，取消后由各任务自己的上下文负责关闭。
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
