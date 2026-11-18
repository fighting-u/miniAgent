import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from functools import partial
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from src.config import Config
from src.tools.tool import Tool, ToolContext


# MCP 连接与工具发现的总等待时间，以及单次工具调用的等待时间。
MCP_CONNECT_TIMEOUT = 30
MCP_CALL_TIMEOUT = 30
# HTTP 首次连接最多尝试三次，包含第一次连接。
MCP_HTTP_CONNECT_ATTEMPTS = 3
# 当前模块诊断日志记录器。
logger = logging.getLogger(__name__)


def normalize_mcp_name(name: str) -> str:
    """统一修正 MCP 服务器名和工具名，将非常规字符逐个替换为下划线。"""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name) or "_"


class MCPManager:
    """维护 stdio 或 Streamable HTTP 连接，发现并适配 MCP 工具。"""

    def __init__(self, config: Config) -> None:
        """保存统一配置和连接任务，不再自行读取另一个配置文件。"""
        self._servers = config.mcp_servers
        self._tasks: list[asyncio.Task[None]] = []  # 每个任务持有一个服务器的连接。

    async def start(self) -> list[Tool]:
        """并行连接服务器，返回已绑定连接的工具供 ToolManager 注册。"""
        # 每个连接任务自行进入和退出 SDK 上下文，避免跨任务关闭 AnyIO 取消作用域。
        pending: list[asyncio.Future[list[Tool]]] = []
        loop = asyncio.get_running_loop()
        seen_names = set()
        for original_name, settings in self._servers.items():
            name = normalize_mcp_name(original_name)
            if name in seen_names:
                logger.warning("MCP 服务器 %s 修正后名称 %s 重复，保留第一项", original_name, name)
                continue
            seen_names.add(name)
            ready: asyncio.Future[list[Tool]] = loop.create_future()
            self._tasks.append(asyncio.create_task(self._serve(name, settings, ready)))
            pending.append(ready)

        tools = []
        seen_tools = set()
        for definitions in await asyncio.gather(*pending):
            names = {tool.name for tool in definitions}
            if conflicts := seen_tools & names:
                # 服务器 a__b 的工具 c，与服务器 a 的工具 b__c，都会得到 mcp__a__b__c；太奇葩了，应该很少出现
                print("MCP 工具名称与其他服务器冲突，跳过该服务器的工具。")
                logger.warning("跳过 MCP 重名工具：%s", conflicts)
                continue
            tools.extend(definitions)
            seen_tools.update(names)
        return tools

    @asynccontextmanager
    async def _transport(self, transport: str, settings: dict[str, Any]):
        """按配置选择传输，在连接所属任务内统一管理传输和 HTTP 客户端。"""
        if transport == "stdio":
            if "url" in settings:
                raise ValueError("stdio 配置不能包含 url")
            async with stdio_client(StdioServerParameters(**settings)) as streams:
                yield streams
        elif transport in ("http", "streamable-http"):
            url = settings.get("url", "")
            address = urlsplit(url) if isinstance(url, str) else None
            if address is None or address.scheme not in {"http", "https"} or not address.netloc:
                raise ValueError("HTTP MCP 必须配置有效的 http/https url")
            if "command" in settings:
                raise ValueError("HTTP 配置不能包含 command")
            headers = settings.get("headers", {})
            if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
                raise ValueError("headers 必须是字符串键值对象")
            async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(MCP_CALL_TIMEOUT, connect=MCP_CONNECT_TIMEOUT)) as client:
                async with streamable_http_client(url, http_client=client) as (read, write, _):
                    yield read, write
        else:
            raise ValueError(f"不支持的 MCP 传输类型：{transport}")

    async def _serve(self, name: str, settings: Any, ready: asyncio.Future[list[Tool]]) -> None:
        """管理单个服务器的连接尝试和故障日志。"""
        try:
            if not isinstance(settings, dict):
                raise ValueError("服务器配置必须是对象")
            transport = settings.get("type", "http" if "url" in settings else "stdio")
            attempts = MCP_HTTP_CONNECT_ATTEMPTS if transport in ("http", "streamable-http") else 1
            for attempt in range(1, attempts + 1):
                try:
                    logger.info("MCP 服务器 %s 开始连接，传输类型：%s", name, transport)
                    await self._connect(name, transport, settings, ready)
                    return
                except Exception as error:
                    # 工具发布后不再重连，避免重新绑定已交付给 Agent 的执行入口。
                    if ready.done() or attempt == attempts:
                        raise
                    logger.warning("MCP HTTP 服务器 %s 首次连接失败（%d/%d），准备重试：%s",
                                   name, attempt, attempts, error, exc_info=True)
        except Exception as error:
            # 保留完整堆栈及 TaskGroup 的子异常，避免只有异常组摘要而无法定位原因。
            logger.warning("MCP 服务器 %s 连接或运行失败：%s", name, error, exc_info=True)
            print(f"MCP 服务器 {name} 不可用：{error}")
        finally:
            if not ready.done():
                ready.set_result([])

    async def _connect(
        self, name: str, transport: str, settings: dict[str, Any], ready: asyncio.Future[list[Tool]],
    ) -> None:
        """执行一次连接及工具发现，发布成功后保持连接直到退出。"""
        async with (
            self._transport(transport, settings) as (read, write),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=MCP_CALL_TIMEOUT)) as session,
        ):
            with anyio.fail_after(MCP_CONNECT_TIMEOUT):
                await session.initialize()
                tools = await self._discover_tools(name, session)
            if not ready.done():
                ready.set_result(tools)
            logger.info("MCP 服务器 %s 工具发现完成，共 %d 个工具", name, len(tools))
            # 保持连接，直到 stop 取消当前任务并触发上下文退出。
            await asyncio.Future()

    async def _discover_tools(self, name: str, session: ClientSession) -> list[Tool]:
        """分页读取服务器工具，并绑定当前连接和工具原名。"""
        tools: dict[str, Tool] = {}
        cursor = None
        seen_cursors = set()
        while True:
            page = await session.list_tools(cursor=cursor)
            for tool in page.tools:
                # 完整名称最多 64 字符；修正或截断造成的重名沿用原冲突处理。
                exposed = f"mcp__{name}__{normalize_mcp_name(tool.name)}"[:64]
                if exposed in tools:
                    raise ValueError(f"工具名称重复：{exposed}")
                tools[exposed] = Tool(
                    name=exposed,
                    description=tool.description or f"调用 {name} 的 {tool.name} 工具",
                    parameters=tool.inputSchema,
                    execute=partial(self._call_tool, session, tool.name),
                    strict=False,
                )
            cursor = page.nextCursor
            if cursor is None:
                return list(tools.values())
            if cursor in seen_cursors:
                raise ValueError("工具列表分页游标重复")
            seen_cursors.add(cursor)

    async def _call_tool(
        self, session: ClientSession, name: str, arguments: dict[str, Any], _context: ToolContext,
    ) -> str:
        """通过绑定的连接和原始名称调用工具，沿用现有 MCP 结果格式。"""
        try:
            result = await session.call_tool(name, arguments)
            content = [
                block.model_dump(mode="json", by_alias=True, exclude_none=True)
                if block.type == "text" else {"type": block.type, "text": "暂不支持此类型的 MCP 内容"}
                for block in result.content
            ]
            return json.dumps({"content": content, "structuredContent": result.structuredContent,
                               "isError": result.isError}, ensure_ascii=False)
        except Exception as error:
            logger.warning("MCP 工具 %s 调用失败：%s", name, error)
            return json.dumps({"isError": True, "error": f"MCP 调用失败：{error}"}, ensure_ascii=False)

    async def stop(self) -> None:
        """统一取消任务，等待 SDK 关闭 stdio 子进程或 HTTP 会话及客户端。"""
        # todo:这里有必要等待吗，直接关了也行？
        # cancel 仅请求停止；仍需 gather 等待上下文清理实际完成。
        # 同时终止仍在握手或发现工具的任务，取消后由各任务自己的上下文负责关闭。
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
