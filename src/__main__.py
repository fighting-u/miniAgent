"""常驻程序入口：终端客户端通过 python -m src.ui.terminal 单独启动。"""

import asyncio
import logging

from src.common.logging_config import setup_logging
from src.config import Config
from src.runtime import AgentRuntime


# 常驻程序的启动与资源回收日志。
logger = logging.getLogger(__name__)


async def serve(config: Config) -> None:
    """启动运行时，通道与模型资源均由 Runtime 管理。"""
    log_path = setup_logging()
    logger.info("MiniAgent 常驻程序启动，本次日志文件：%s", log_path)
    runtime = AgentRuntime(config)
    try:
        await runtime.start()
        print("常驻程序运行中，另开终端执行 python -m src.ui.terminal；Ctrl+C 停止。", flush=True)
        await asyncio.Future()
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
