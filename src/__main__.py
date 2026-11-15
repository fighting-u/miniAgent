"""常驻程序入口：终端客户端通过 python -m src.terminal 单独启动。"""

import asyncio
import logging

from src.channels.manager import ChannelManager
from src.common.logging_config import setup_logging
from src.config import Config
from src.runtime.runtime import AgentRuntime


# 常驻程序的启动与资源回收日志。
logger = logging.getLogger(__name__)


async def serve(config: Config) -> None:
    """组装运行时和通道管理器，资源初始化及回收均交给对应对象。"""
    log_path = setup_logging()
    logger.info("MiniAgent 常驻程序启动，本次日志文件：%s", log_path)
    runtime = AgentRuntime(config)
    manager = ChannelManager(runtime)
    try:
        await runtime.start()
        await manager.start()
        for channel in runtime.dispatcher.channels.values():
            print(f"通道已就绪：{channel.channel_id} {getattr(channel, 'uri', '')}", flush=True)
        print("常驻程序运行中，另开终端执行 python -m src.terminal；Ctrl+C 停止。", flush=True)
        await asyncio.Future()
    finally:
        try:
            # 先关闭入口及审批等待，再完成已接受的请求和后台记忆任务。
            await manager.stop()
        finally:
            await runtime.stop()


def main() -> None:
    """读取一次集中配置；__main__ 同时承担命令行入口和常驻服务组装。"""
    # 固定读取当前启动目录下的 config.yaml。
    try:
        asyncio.run(serve(Config.load()))
    except KeyboardInterrupt:
        print("常驻程序已停止。")


if __name__ == "__main__":
    main()
