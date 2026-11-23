import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# 项目根目录作为正式运行的默认路径基准，不依赖启动命令所在目录。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 默认配置文件名；密钥仍单独保存在未纳入版本控制的 .env 中。
DEFAULT_CONFIG_PATH = "config.yaml"


@dataclass
class Config:
    """集中保存可配置的运行参数；业务模块显式接收同一实例。"""

    model: str = ""  # 主模型、记忆辅助模型和压缩模型共用的名称。
    base_url: str | None = None  # OpenAI 兼容接口地址；为空时使用 SDK 默认地址。
    api_key_env: str = "DASHSCOPE_API_KEY"  # 从环境读取模型密钥的变量名。
    api_key: str = field(default="", repr=False)  # 已解析的模型密钥，不出现在配置对象的打印结果中。
    allowed_dirs: tuple[Path, ...] = ()  # 本轮当前目录之外，权限规则允许访问的目录。
    max_concurrent_requests: int = 10  # 同时处理请求的最大数量。
    session_owner_ttl: int = 60  # 当前轮执行结束后，原来源继续占用的秒数。
    powershell_timeout: int = 120  # PowerShell 工具的单次执行秒数。
    channels: list[dict[str, Any]] = field(default_factory=list, repr=False)  # 通道实例配置，解析后可能包含密钥。
    mcp_servers: dict[str, Any] = field(default_factory=dict, repr=False)  # MCP 服务器配置，可能包含认证信息。
    model_hosted_tools: list[str] = field(default_factory=list)  # 由模型服务端执行的工具类型。
    long_term_memory_enabled: bool = field(kw_only=True)  # 控制长期记忆目录、工具、召回和提取。
    effective_window_percent: int = field(kw_only=True)  # 有效上下文窗口占原始窗口的百分比（1~100）。
    auto_compact_percent: int = field(kw_only=True)  # 自动触发上下文压缩的百分比阈值（1~100）。
    compact_keep_user_tokens: int = field(kw_only=True)  # 压缩重建历史时保留用户原文的 token 预算上限。
    tool_output_max_tokens: int = field(kw_only=True)  # 单个工具文本输出块的 token 上限，超出时截断并转存完整内容。

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> "Config":
        """读取 YAML 与本地环境，可配置的相对路径以所选配置文件的目录为基准。"""
        path = Path(path).resolve()
        load_dotenv(PROJECT_ROOT / ".env")
        settings = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise ValueError("config.yaml 必须是配置对象")
        unknown = settings.keys() - {item.name for item in fields(cls)}
        if unknown:
            raise ValueError(f"未知配置项：{'、'.join(sorted(unknown))}")
        if "api_key" in settings:
            raise ValueError("请通过 api_key_env 指定密钥环境变量，不要把密钥写入 YAML")
        config = cls(**settings)
        config.allowed_dirs = tuple((path.parent / Path(value).expanduser()).resolve() for value in config.allowed_dirs)
        if type(config.max_concurrent_requests) is not int or config.max_concurrent_requests <= 0:
            raise ValueError("max_concurrent_requests 必须为正整数")
        if type(config.session_owner_ttl) is not int or config.session_owner_ttl < 0:
            raise ValueError("session_owner_ttl 必须为非负整数")
        if type(config.powershell_timeout) is not int or config.powershell_timeout <= 0:
            raise ValueError("powershell_timeout 必须为正整数")
        if type(config.long_term_memory_enabled) is not bool:
            raise ValueError("long_term_memory_enabled 必须为布尔值")
        if type(config.effective_window_percent) is not int or not 1 <= config.effective_window_percent <= 100:
            raise ValueError("effective_window_percent 必须为 1 到 100 之间的正整数")
        if type(config.auto_compact_percent) is not int or not 1 <= config.auto_compact_percent <= 100:
            raise ValueError("auto_compact_percent 必须为 1 到 100 之间的正整数")
        if config.auto_compact_percent > config.effective_window_percent:
            raise ValueError("auto_compact_percent 不能大于 effective_window_percent")
        if type(config.compact_keep_user_tokens) is not int or config.compact_keep_user_tokens <= 0:
            raise ValueError("compact_keep_user_tokens 必须为正整数")
        if type(config.tool_output_max_tokens) is not int or config.tool_output_max_tokens <= 0:
            raise ValueError("tool_output_max_tokens 必须为正整数")
        if not isinstance(config.model, str) or not isinstance(config.api_key_env, str):
            raise ValueError("model 和 api_key_env 必须是文本")
        if config.base_url is not None and not isinstance(config.base_url, str):
            raise ValueError("base_url 必须是文本或 null")
        if not isinstance(config.channels, list) or not isinstance(config.mcp_servers, dict):
            raise ValueError("channels 必须是列表，mcp_servers 必须是对象")
        seen = set()
        for item in config.channels:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                raise ValueError("每个通道必须配置有效的 id")
            if item["id"] in seen or not isinstance(item.get("type"), str):
                raise ValueError(f"通道 ID 重复或类型无效：{item['id']}")
            if not isinstance(item.get("options", {}), dict):
                raise ValueError(f"通道 options 必须是对象：{item['id']}")
            seen.add(item["id"])
            if item["type"] == "qq":
                options = item.setdefault("options", {})
                # YAML 只存变量名；各实例可使用不同环境变量，QQ 类直接接收解析后的凭据。
                options["app_id"] = os.getenv(options.pop("app_id_env", "QQ_APP_ID"), "").strip()
                options["secret"] = os.getenv(options.pop("secret_env", "QQ_APP_SECRET"), "").strip()
        config.api_key = os.getenv(config.api_key_env, "")
        return config

    @property
    def terminal_url(self) -> str:
        """根据终端通道配置取得独立客户端的默认连接地址。"""
        for channel in self.channels:
            if channel["type"] == "terminal" and channel.get("enabled", True):
                options = channel["options"]
                host = options["host"]
                host = "127.0.0.1" if host == "0.0.0.0" else host
                return f"ws://{host}:{options['port']}"
        raise ValueError("配置文件中没有启用的终端通道")
