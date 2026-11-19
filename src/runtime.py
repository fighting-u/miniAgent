import asyncio
import logging
from pathlib import Path

from openai import AsyncOpenAI

from src.channels.base import ChannelDisconnected, InboundMessage, MessageDispatcher
from src.channels.manager import ChannelManager
from src.config import Config, PROJECT_ROOT
from src.message_process.message_processor import MessageProcessor
from src.session.session_manager import SessionManager
from src.session.storage import SESSION_DIR_NAME
from src.tools.tool_manager import ToolManager

# 记录常驻服务的启停和任务收尾。
logger = logging.getLogger(__name__)


class AgentRuntime:
    """组装共享资源并协调请求任务与常驻服务的生命周期。"""

    def __init__(self, config: Config) -> None:
        """保存统一配置，管理 Agent 资源、通道、会话与事件分发的完整生命周期。"""
        self.config = config
        if not config.api_key or not config.model.strip():
            raise ValueError(f"请在 config.yaml 配置 model，并设置 {config.api_key_env} 环境变量")
        self.client = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url)  # 构造客户端，stop 时关闭。
        self.tool_manager = ToolManager(config)  # 共享工具管理器，start 时注册工具并建立 MCP 连接。
        self.requests: dict[asyncio.Task, InboundMessage | ChannelDisconnected] = {}  # 跟踪已接受的输入任务，退出时等待完成。
        self.message_dispatcher = MessageDispatcher()
        self.session_manager = SessionManager(PROJECT_ROOT / SESSION_DIR_NAME, config)  # 管理会话历史、存储和入口绑定。
        self.channels = ChannelManager(config, self.message_dispatcher, self.submit, self.session_manager)  # 共享配置和消息分发器，并传入提交回调。
        # 保留本进程创建的后台记忆任务，退出前显式等待并收集异常。
        self.background_tasks: list[asyncio.Task] = []
        # todo：传那么多参数，是否会太乱以及不易维护？配置集中传入，资源由runtime管理。
        self.message_processor = MessageProcessor(
            config=config,
            client=self.client,
            tool_manager=self.tool_manager,
            message_dispatcher=self.message_dispatcher,
            session_manager=self.session_manager,
            requests=self.requests,
            background_tasks=self.background_tasks,
        )

    async def start(self) -> None:
        """会话按入口选择懒加载，先准备工具再启动通道。"""
        # 通道收到输入后立即提交，先准备工具，保证请求执行时工具已就绪。
        await self.tool_manager.start()
        await self.channels.start()
        logger.info("AgentRuntime 已启动：model=%s cwd=%s", self.config.model, Path.cwd().resolve())

    def submit(self, message: InboundMessage | ChannelDisconnected) -> None:
        """只创建和登记输入任务，提交后立即返回，不等待业务处理。"""
        task = asyncio.create_task(self.message_processor.process_message(message), name=f"message-{message.channel_id}")
        self.requests[task] = message
        task.add_done_callback(self.requests.pop)

    async def stop(self) -> None:
        """停止通道、完成请求和后台任务后关闭工具和模型客户端，允许启动失败后调用。"""
        try:
            try:
                await self.channels.stop()
            finally:
                await self._drain()
        finally:
            try:
                await self.tool_manager.stop()
            finally:
                await self.client.close()
        logger.info("AgentRuntime 已停止，共享模型与 MCP 资源已关闭")

    async def _drain(self) -> None:
        """等待已接受请求和后台记忆任务，保持客户端可用到最后。"""
        while self.requests:
            await asyncio.gather(*list(self.requests), return_exceptions=True)
        if self.background_tasks:
            results = await asyncio.gather(*self.background_tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    logger.warning("后台记忆提取任务未正常完成：%s", result)
            self.background_tasks.clear()
