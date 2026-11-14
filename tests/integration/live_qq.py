"""只验证 QQ 官方鉴权、网关 READY 和心跳，不调用模型、不发送私聊消息。"""

import asyncio
import logging
import os
import sys
from pathlib import Path


# 支持从项目根目录直接运行本测试脚本。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from src.channels.manager import ChannelManager, load_config
from src.runtime.permissions import ApprovalBroker
from src.channels.base import Dispatcher
from tests.temp_workspace import temporary_workspace


async def check_connection() -> None:
    """复用正式配置与通道管理器，成功或异常都关闭连接并删除临时日志。"""
    load_dotenv(PROJECT_ROOT / ".env")
    settings = load_config()
    settings["channels"] = [item for item in settings["channels"]
                            if item["type"] == "qq" and item.get("enabled", True)]
    if not settings["channels"]:
        raise ValueError("请先在 .channels.yaml 中启用 QQ 通道")
    baseline = set(asyncio.all_tasks())
    dispatcher = Dispatcher()
    manager = ChannelManager(asyncio.Queue(), ApprovalBroker(dispatcher), dispatcher)
    with temporary_workspace("miniagent-qq-") as root:
        # 日志只保存在本次临时目录，不输出请求头或原始平台响应。
        handler = logging.FileHandler(root / "connection.log", encoding="utf-8")
        progress = logging.StreamHandler(sys.stdout)
        progress.addFilter(logging.Filter("src.channels.qq"))
        root_logger = logging.getLogger()
        previous_level = root_logger.level
        channel_logger = logging.getLogger("src.channels.qq")
        previous_channel_level = channel_logger.level
        channel_logger.setLevel(logging.DEBUG)
        root_logger.addHandler(handler)
        root_logger.addHandler(progress)
        root_logger.setLevel(logging.INFO)
        try:
            await manager.start(settings)
            for channel in dispatcher.channels.values():
                await asyncio.wait_for(channel.heartbeat_ack.wait(), 20)
                if not channel.connected or channel.task.done():
                    raise AssertionError("QQ 网关未保持连接")
                print(f"QQ 连接成功：{channel.channel_id}，已完成 REST 鉴权、网关 READY、心跳 ACK。")
        finally:
            await manager.stop()
            root_logger.removeHandler(handler)
            root_logger.removeHandler(progress)
            root_logger.setLevel(previous_level)
            channel_logger.setLevel(previous_channel_level)
            handler.close()
            progress.close()
        remaining = {task for task in asyncio.all_tasks() - baseline if not task.done()}
        if remaining:
            raise AssertionError(f"QQ 关闭后仍有 {len(remaining)} 个后台任务")
    print("测试连接已关闭，临时日志已清理；未调用模型或发送私聊消息。")


def main() -> None:
    """报告测试结果，错误输出遮盖本地凭据，失败也不保留临时文件。"""
    try:
        asyncio.run(check_connection())
    except Exception as error:
        text = str(error)
        for name in ("QQ_APP_SECRET", "QQ_APP_ID"):
            value = os.getenv(name)
            if value:
                text = text.replace(value, "[已隐藏]")
        print(f"QQ 连接测试失败：{type(error).__name__}: {text}", file=sys.stderr)
        print("测试连接与临时产物已清理。", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
