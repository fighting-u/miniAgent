import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

from live_mcp import PROJECT_ROOT, SERVER_SOURCE, run_test


# 复用单工具服务；在子进程内绑定随机端口，避免固定端口冲突。
HTTP_SERVER_SOURCE = SERVER_SOURCE.replace('    server.run(transport="stdio")', '''    import socket
    import uvicorn

    # ASGI 应用记录鉴权和会话删除，用于验证 headers 以及连接关闭。
    app = server.streamable_http_app()

    async def audited_app(scope, receive, send):
        """校验测试请求头，并记录 SDK 是否发送会话终止请求。"""
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if headers.get(b"authorization") != b"Bearer smoke-test":
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
            if scope["method"] == "DELETE":
                root.joinpath("deleted.txt").write_text("closed", encoding="utf-8")
        await app(scope, receive, send)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    root.joinpath("url.txt").write_text(f"http://127.0.0.1:{listener.getsockname()[1]}/mcp", encoding="utf-8")
    uvicorn.Server(uvicorn.Config(audited_app, log_level="warning")).run(sockets=[listener])''')


async def run_http_test(root: Path) -> None:
    """启动真实 HTTP MCP，复用主 LLM 单次调用断言，最后关闭本次服务进程。"""
    script = root / "http_server.py"
    script.write_text(HTTP_SERVER_SOURCE, encoding="utf-8")
    with (root / "http_server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([sys.executable, "-B", str(script), str(root)],
            stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        url = None
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            for _ in range(100):
                if process.poll() is not None:
                    raise AssertionError("临时 HTTP 服务提前退出：" + (root / "http_server.log").read_text(encoding="utf-8"))
                if (root / "url.txt").exists():
                    url = (root / "url.txt").read_text(encoding="utf-8")
                    try:
                        response = await client.get(url)
                        if response.status_code == 401:
                            break
                    except httpx.TransportError:
                        pass
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("HTTP MCP 启动超时")
        await run_test(root, {"type": "http", "url": url,
                              "headers": {"Authorization": "Bearer smoke-test"}})
        assert (root / "deleted.txt").read_text(encoding="utf-8") == "closed", "未发送 HTTP 会话终止请求"
        assert process.poll() is None, "关闭客户端不应终止外部 HTTP 服务"
        print("HTTP 鉴权请求头及会话 DELETE 关闭验证通过。")
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        print("临时 HTTP MCP 服务进程已退出。")


def main() -> None:
    """测试成功或失败均删除临时服务、配置、日志和会话文件。"""
    root = None
    try:
        with tempfile.TemporaryDirectory(prefix="miniagent-mcp-http-", dir=PROJECT_ROOT / "tests") as directory:
            root = Path(directory)
            asyncio.run(run_http_test(root))
    finally:
        if root is not None:
            assert not root.exists(), "临时目录未清理"
            print("HTTP 测试临时程序和文件已全部清理。")


if __name__ == "__main__":
    main()
