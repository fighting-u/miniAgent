# MiniAgent
嗨，这里是轻量融合codex、claude code、openclaw这些优秀agent长处的简化版常驻型agent，面向个人用户，方便扩展二次开发。

现已支持qq，终端双通道，新增自定义通道只需继承 src/channels/base.py 中的 Channel 基类开发，在 src/channels/manager.py 的 CHANNEL_TYPES 中注册类型，再在 config.yaml 的 channels 中添加配置即可使用。

核心循环代码在 `src/message_process/turn_runner.py` 的 `TurnRunner.run_turn` 方法中。

## 项目结构

```text
miniAgent/
├── src/                        # 源代码入口
│   ├── __main__.py             # python -m src 启动入口
│   ├── config.py               # Config 配置类，负责加载 config.yaml 与 .env
│   ├── runtime.py              # AgentRuntime 运行时，管理生命周期、依赖组装和任务调度
│   ├── channels/               # 多聊天通道协议基类与实现类
│   ├── common/                 # 公共工具函数与日志配置
│   ├── compact/                # 上下文压缩
│   ├── hook/                   # 生命周期 Hook 注册与触发扩展
│   ├── memory/                 # 长期记忆提取、存储与按需召回
│   ├── message_process/        # 入口消息处理、斜杠命令与 Agent 轮次循环
│   ├── models/                 # 模型静态元信息注册表（如上下文窗口配置）
│   ├── prompt/                 # 系统提示词、压缩提示词与记忆提示词模板
│   ├── security/               # 工具安全检查、高危命令拦截与审批控制
│   ├── session/                # 会话状态管理与 JSONL 持久化存储
│   ├── tools/                  # 内置工具、技能加载与 MCP 工具接入管理
│   └── ui/                     # 独立终端客户端及交互界面
├── config.yaml.example         # 运行配置示例模板（复制为 config.yaml 后使用）
├── .env.example                # 环境变量与密钥示例模板（复制为 .env 后使用）
├── skills/                     # 本地技能目录，按需加载技能说明
├── docs/                       # 项目设计文档与学习记录
├── tests/                      # 单元测试与真实接口集成测试
├── sessions/                   # 运行时生成的会话 JSONL 与入口绑定目录
├── .memory/                    # 运行时生成的长期记忆目录
├── logs/                       # 运行时日志目录
├── pyproject.toml              # 项目元数据与 Python 依赖配置
├── README.md                   # 项目说明文档
└── AGENTS.md                   # 项目开发与协作约定
```

## 开始

安装依赖

```powershell
uv sync
```

参考配置文件示例[.env.example](.env.example)和[config_example.yaml](config_example.yaml)，填充自己的配置（实际配置用 `.env` 与 `config.yaml`）：

启动程序

```powershell
# 第一个终端：启动常驻程序
python -m src

# 第二个终端：连接常驻程序并聊天
python -m src.ui.terminal
```

qq需要先去qq机器人平台注册获取到QQ_APP_ID，QQ_APP_SECRET后放入.env中；

可用斜杠命令

- `/new`：开启新会话
- `/resume`：查看历史会话列表；`/resume <编号>` 恢复指定会话
- `/compact`：手动压缩当前会话上下文
- `/delete`：删除历史会话
- `/q`：退出终端客户端