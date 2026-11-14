import argparse
import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

from src.channels.manager import DEFAULT_CONFIG, ChannelManager, load_config
from src.common.logging_config import setup_logging
from src.runtime.runner import AgentRunner
from src.runtime.runtime import Runtime
from src.session.session_store import SESSION_DIR
from src.tools.mcp_client import MCPClient
from src.tools.tools import initialize_tools


# 常驻程序的启动与资源回收日志。
logger = logging.getLogger(__name__)


async def serve(config_path: Path = DEFAULT_CONFIG, session_root: Path = SESSION_DIR) -> None:
    """启动共享资源和通道，持续运行到进程取消。"""
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    log_path = setup_logging()
    logger.info("MiniAgent 常驻程序启动，本次日志文件：%s", log_path)
    settings = load_config(config_path)
    base_url = os.getenv("BASE_URL")
    # api_key = os.getenv("OPENAI_API_KEY")
    # qwen
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("未找到 API_KEY，请先在 .env 中配置。")
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    # 启动即连接 MCP，与终端连接和辅助模型召回并行。
    mcp = MCPClient()
    # todo: 后续还是改成新线程初始化，异步的话，用户输入字符等好几秒后才会显示，还以为卡住了
    tools_ready = asyncio.create_task(initialize_tools(mcp))
    runtime = Runtime(AgentRunner(client, mcp, tools_ready), session_root, settings.get("queue_size", 100))
    manager = ChannelManager(runtime.queue, runtime.approvals, runtime.dispatcher)
    try:
        await runtime.start()
        await manager.start(settings)
        for channel in runtime.dispatcher.channels.values():
            print(f"通道已就绪：{channel.channel_id} {getattr(channel, 'uri', '')}", flush=True)
        print("常驻程序运行中，另开终端执行 python -m src.terminal；Ctrl+C 停止。", flush=True)
        await asyncio.Future()
    finally:
        try:
            # 先关闭入口及审批等待，再完成已接受的请求和后台记忆任务。
            await manager.stop()
            await runtime.stop()
        finally:
            # 所有后台请求结束后再关闭共享客户端，随后关闭事件循环。
            try:
                await mcp.close(tools_ready)
            finally:
                await client.close()


def main() -> None:
    """解析常驻端启动参数，不创建任何终端聊天界面。"""
    parser = argparse.ArgumentParser(description="MiniAgent 常驻程序")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="通道配置文件路径")
    args = parser.parse_args()
    try:
        asyncio.run(serve(args.config))
    except KeyboardInterrupt:
        print("常驻程序已停止。")
