import argparse
import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from src.ui.terminal_ui import TerminalUI
from src.config import Config, DEFAULT_CONFIG_PATH


class TerminalClient:
    """通过 WebSocket 对话，仅持有会话标识和本地界面。"""

    def __init__(self, url: str, ui: TerminalUI | None = None) -> None:
        """允许测试注入界面；正常启动时等待服务端返回模型信息后创建。"""
        self.url = url
        self.ui = ui
        self.session_id = ""
        self.connection: Any = None

    async def request(self, text: str) -> list[dict[str, Any]]:
        """发送一次输入，按序处理事件直到本次请求结束。"""
        request_id = uuid4().hex
        await self.connection.send(json.dumps({"request_id": request_id, "session_id": self.session_id,
                                              "input": text}, ensure_ascii=False))
        events = []
        while True:
            event = json.loads(await self.connection.recv())
            if event.get("request_id", request_id) != request_id:
                continue
            events.append(event)
            kind = event["type"]
            if kind == "session":
                self.session_id = event["session_id"]
                if self.ui is None:
                    self.ui = TerminalUI(event.get("model", ""), event.get("workdir"))
                self.ui.show_session(event["items"], event["title"])
            elif kind == "items":
                self.ui.show_items(event["items"])
            elif kind in {"error", "status"}:
                print(event["text"])
            elif kind == "approval":
                allowed = await self.ui.confirm_permission(event)
                await self.connection.send(json.dumps({
                    "input": f"/approve {event['approval_id']} {'allow' if allowed else 'deny'}",
                    "session_id": self.session_id,
                }))
            elif kind == "done":
                return events

    async def run(self) -> None:
        """连接常驻端，保留原终端输入、选择菜单和二次确认操作。"""
        # 本机连接不走环境代理；退出客户端只关闭连接，不取消常驻端任务。
        async with connect(self.url, proxy=None, max_size=None) as connection:
            self.connection = connection
            await self.request("/attach")
            while True:
                try:
                    text = await self.ui.read_input()
                except (EOFError, KeyboardInterrupt):
                    break
                if not text:
                    continue
                if text == "/q":
                    break
                if text in {"/resume", "/delete"}:
                    events = await self.request(text)
                    entries = next((event["sessions"] for event in events if event["type"] == "sessions"), [])
                    selected = await self.ui.choose_session(entries, action="删除" if text == "/delete" else "恢复")
                    if selected is None:
                        continue
                    if text == "/delete":
                        info = next(entry for entry in entries if entry["id"] == selected)
                        if not await self.ui.confirm_delete(info, selected == self.session_id):
                            print("已取消删除。")
                            continue
                    text = f"{text} {selected}" + (" confirm" if text == "/delete" else "")
                await self.request(text)
        print("终端已断开，常驻程序继续运行。")


def main() -> None:
    """解析终端连接地址，运行独立聊天客户端。"""
    parser = argparse.ArgumentParser(description="MiniAgent 终端客户端")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="统一配置文件路径")
    parser.add_argument("--url", help="覆盖配置中的常驻端 WebSocket 地址")
    args = parser.parse_args()
    try:
        config = Config.load(args.config)
        asyncio.run(TerminalClient(args.url or config.terminal_url).run())
    except (OSError, ConnectionClosed) as error:
        print(f"无法继续连接常驻程序：{error}")
    except KeyboardInterrupt:
        print("终端已断开，常驻程序继续运行。")


if __name__ == "__main__":
    main()
