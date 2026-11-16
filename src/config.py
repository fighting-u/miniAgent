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
    workdir: Path = PROJECT_ROOT  # 文件工具和 PowerShell 使用的工作区。
    extra_allowed_dirs: tuple[Path, ...] = ()  # 工作区之外额外允许访问的目录。
    max_concurrent_requests: int = 10  # 同时处理请求的最大数量。
    powershell_timeout: int = 120  # PowerShell 工具的单次执行秒数。
    channels: list[dict[str, Any]] = field(default_factory=list, repr=False)  # 通道实例配置，解析后可能包含密钥。
    mcp_servers: dict[str, Any] = field(default_factory=dict, repr=False)  # MCP 服务器配置，可能包含认证信息。

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
        config.workdir = (path.parent / Path(config.workdir).expanduser()).resolve()
        config.extra_allowed_dirs = tuple((path.parent / Path(value).expanduser()).resolve()
                                          for value in config.extra_allowed_dirs)
        if type(config.max_concurrent_requests) is not int or config.max_concurrent_requests <= 0:
            raise ValueError("max_concurrent_requests 必须为正整数")
        if type(config.powershell_timeout) is not int or config.powershell_timeout <= 0:
            raise ValueError("powershell_timeout 必须为正整数")
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
