import logging
import sys
from datetime import datetime
from pathlib import Path


# 日志文件保存目录。
LOG_DIR = Path.cwd().resolve() / "logs"

# 控制台和文件共同使用的最低日志等级。
LOG_LEVEL = logging.INFO

# 日志时间、等级、模块名和正文的统一输出格式。
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

# 当前进程已经创建的日志文件路径，防止重复初始化 Handler。
CURRENT_LOG_PATH: Path | None = None


def setup_logging() -> Path:
    """初始化控制台和 UTF-8 文件双写日志，并返回本次日志文件路径。"""
    global CURRENT_LOG_PATH
    if CURRENT_LOG_PATH is not None:
        return CURRENT_LOG_PATH

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    CURRENT_LOG_PATH = LOG_DIR / f"mini_agent_{timestamp}.log"

    formatter = logging.Formatter(LOG_FORMAT)

    # 控制台 Handler 继续向当前终端展示运行日志。
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(LOG_LEVEL)
    console_handler.setFormatter(formatter)

    # 文件 Handler 保存本次运行的完整诊断日志。
    file_handler = logging.FileHandler(CURRENT_LOG_PATH, encoding="utf-8")
    file_handler.setLevel(LOG_LEVEL)
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(LOG_LEVEL)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # 降低第三方 HTTP 客户端的常规请求日志，避免掩盖 Agent 自身事件。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    return CURRENT_LOG_PATH
