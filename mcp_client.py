import asyncio
import json
import logging
import re
from datetime import timedelta
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


# MCP 连接与工具发现的总等待时间，以及单次工具调用的等待时间。
MCP_CONNECT_TIMEOUT = 30
MCP_CALL_TIMEOUT = 30
# HTTP 首次连接最多尝试三次，包含第一次连接。
MCP_HTTP_CONNECT_ATTEMPTS = 3
# 默认配置位于项目根目录；缺失时仅使用内置工具。
MCP_CONFIG_PATH = Path(__file__).resolve().parent / ".mcp.json"
# 当前模块诊断日志记录器。
logger = logging.getLogger(__name__)


def normalize_mcp_name(name: str) -> str:
    """统一修正 MCP 服务器名和工具名，将非常规字符逐个替换为下划线。"""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name) or "_"


def first_config_values(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """解析配置时保留重复键的第一项，避免同名服务器被后面的配置覆盖。"""
    values = {}
    for key, value in pairs:
        if key in values:
            logger.warning("MCP 配置键 %s 重复，保留第一项", key)
            continue
        values[key] = value
    return values


class MCPClient:
    """维护 stdio 或 Streamable HTTP 连接、静态工具定义及调用路由。"""

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
            config = json.loads(self.config_path.read_text(encoding="utf-8"), object_pairs_hook=first_config_values)
            servers = config["mcpServers"]
            if not isinstance(servers, dict):
                raise ValueError("mcpServers 必须是对象")
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.warning("MCP 配置读取失败：%s", error)
            print(f"MCP 配置无效，继续使用内置工具：{error}")
            return
        # 每个连接任务自行进入和退出 SDK 上下文，避免跨任务关闭 AnyIO 取消作用域。
        pending = []
        seen_names = set()
        for original_name, settings in servers.items():
            name = normalize_mcp_name(original_name)
            if name in seen_names:
                logger.warning("MCP 服务器 %s 修正后名称 %s 重复，保留第一项", original_name, name)
                continue
            seen_names.add(name)
            ready = asyncio.get_running_loop().create_future()
            self.tasks.append(asyncio.create_task(self._serve(name, settings, ready)))
            pending.append(ready)
        discovered = await asyncio.gather(*pending)
        for definitions, routes in discovered:
            if self.routes.keys() & routes.keys():
                # 服务器 a__b 的工具 c，与服务器 a 的工具 b__c，都会得到 mcp__a__b__c；太奇葩了，应该很少出现
                print("MCP 工具名称与其他服务器冲突，跳过该服务器的工具。")
                logger.warning("跳过 MCP 重名工具：%s", self.routes.keys() & routes.keys())
                continue
            self.tools.extend(definitions)
            self.routes.update(routes)

    @asynccontextmanager
    async def _transport(self, settings: dict[str, Any]):
        """按配置选择传输，在连接所属任务内统一管理传输和 HTTP 客户端。"""
        transport = settings.get("type", "http" if "url" in settings else "stdio")
        logger.info("MCP 开始连接，传输类型：%s", transport)
        if transport == "stdio":
            if "url" in settings:
                raise ValueError("stdio 配置不能包含 url")
            async with stdio_client(StdioServerParameters(**settings)) as streams:
                yield streams
        elif transport in {"http", "streamable-http"}:
            url = settings.get("url", "")
            if not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc:
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

    async def _serve(self, name: str, settings: Any, ready: asyncio.Future) -> None:
        """建立单个连接、发现全部工具，并保持上下文直到收到退出信号。"""
        try:
            if not isinstance(settings, dict):
                raise ValueError("服务器配置必须是对象")
            transport = settings.get("type", "http" if "url" in settings else "stdio")
            attempts = MCP_HTTP_CONNECT_ATTEMPTS if transport in {"http", "streamable-http"} else 1
            for attempt in range(1, attempts + 1):
                try:
                    await self._connect(name, settings, ready)
                    return
                except Exception as error:
                    # 工具发布后不再重连，避免重新绑定已交付给 Agent 的路由。
                    if ready.done() or attempt == attempts:
                        raise
                    logger.warning("MCP HTTP 服务器 %s 首次连接失败（%d/%d），准备重试：%s",
                                   name, attempt, attempts, error)
        except Exception as error:
            logger.warning("MCP 服务器 %s 连接或运行失败：%s", name, error)
            print(f"MCP 服务器 {name} 不可用：{error}")
        finally:
            if not ready.done():
                ready.set_result(([], {}))

    async def _connect(self, name: str, settings: dict[str, Any], ready: asyncio.Future) -> None:
        """执行一次连接及工具发现，发布成功后保持连接直到退出。"""
        async with self._transport(settings) as (read, write):
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
                            # 完整名称最多 64 字符；修正或截断造成的重名沿用原冲突处理。
                            exposed = f"mcp__{name}__{normalize_mcp_name(tool.name)}"[:64]
                            if exposed in routes:
                                raise ValueError(f"工具名称重复：{exposed}")
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
                logger.info("MCP 服务器 %s 工具发现完成，共 %d 个工具", name, len(definitions))
                # 保持连接，直到 close 取消当前任务并触发上下文退出。
                await asyncio.Future()

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
            logger.warning("MCP 工具 %s 调用失败：%s", name, error)
            return json.dumps({"isError": True, "error": f"MCP 调用失败：{error}"}, ensure_ascii=False)

    async def close(self, initialization: asyncio.Task | None = None) -> None:
        """统一取消任务，等待 SDK 关闭 stdio 子进程或 HTTP 会话及客户端。"""
        # todo:这里有必要等待吗，直接关了也行？
        # cancel 仅请求停止；仍需 gather 等待上下文清理实际完成。
        tasks = [*self.tasks] if initialization is None else [initialization, *self.tasks]
        # 同时终止仍在握手或发现工具的任务，取消后由各任务自己的上下文负责关闭。
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
