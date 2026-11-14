# MiniAgent
这是一个学习项目，从零搭建1个agent。模仿codex，claude code，openclaw为主，主要学的是思想，harness工程。

核心代码在`src\agent\core.py`内。

## 项目结构

```text
miniAgent/
├── src/                        # Python 源码包，各子目录通过 __init__.py 标记为包
│   ├── __main__.py             # python -m src 常驻程序启动入口
│   ├── server.py               # 共享资源初始化、通道启动与服务收尾
│   ├── terminal.py             # python -m src.terminal 独立终端客户端
│   ├── channels/               # 通道协议适配
│   │   ├── base.py             # 统一输入、通道基类和 Dispatcher
│   │   ├── manager.py          # 配置读取、类型注册与实例生命周期
│   │   ├── terminal.py         # 常驻端 WebSocket 监听与回复发送
│   │   └── qq.py               # QQ 官方鉴权、网关心跳与好友私聊收发
│   ├── runtime/                # 常驻运行协调
│   │   ├── runtime.py          # 输入队列、单消费者、会话锁和事件持久化
│   │   ├── runner.py           # Agent 执行边界，预留执行池扩展
│   │   └── permissions.py      # 审批等待和控制消息回传
│   ├── agent/                  # Agent 主流程
│   │   ├── core.py             # 对话循环、模型请求和工具调度
│   │   ├── hook.py             # Hook 注册、权限检查和运行日志扩展
│   │   └── system_prompt.py    # 构建模型系统提示词
│   ├── tools/                  # 工具与扩展接入
│   │   ├── tools.py            # 工具初始化、处理函数和任务计划管理
│   │   ├── mcp_client.py       # MCP 连接、工具发现和调用
│   │   └── skill.py            # 本地技能发现与按需加载
│   ├── memory/                 # 长期记忆
│   │   └── memory.py           # 长期记忆提取、存储和召回
│   ├── context/                # 对话上下文压缩
│   │   └── context_compactor.py # 工具结果落盘、历史裁剪和摘要
│   ├── session/
│   │   └── session_store.py    # 会话 JSONL 存储与恢复
│   ├── ui/
│   │   └── terminal_ui.py      # 终端输入、菜单和历史展示
│   └── common/
│       └── logging_config.py   # 文件诊断日志配置
├── .mcp.json                   # MCP 服务器启动配置，默认不连接任何服务器
├── .channels.yaml              # 通道实例配置、终端监听地址与输入队列容量
├── skills/                     # 本地技能目录
│   └── code-review/
│       └── SKILL.md            # 代码审查技能的元数据和完整说明
├── docs/                       # 项目设计与学习记录文档
│   ├── 基础工具设计.md          # 内置工具、工作区路径与权限管控
│   ├── 持久化设计.md            # 会话 JSONL 持久化格式与事件顺序设计
│   ├── 终端运行界面设计.md      # 自动恢复、命令菜单和历史展示约定
│   ├── 多通道设计.md            # 常驻程序、通道队列、事件分发与终端客户端改造方案
│   ├── 记忆压缩设计.md          # 上下文压缩流程、归档和恢复限制
│   ├── 长期记忆设计.md          # 稳定信息的提取、存储和按需召回
│   ├── mcp接入设计.md           # MCP 接入准备与待实现范围
│   └── 记录.md                  # 项目学习记录
├── tests/                      # 测试目录，子目录通过 __init__.py 支持递归发现
│   ├── temp_workspace.py       # 继承工作区权限、自动清理的测试临时目录
│   ├── unit/
│   │   ├── test_channels.py    # WebSocket、终端客户端、队列和事件持久化测试
│   │   ├── test_qq.py          # QQ 网关握手、心跳、私聊路由和资源清理测试
│   │   └── test_sessions.py    # 本地 SDK、工具循环与加载恢复测试
│   └── integration/
│       ├── live_channels.py    # 独立终端、真实模型与工具、多次连接续聊测试
│       ├── live_qq.py          # QQ 官方鉴权、网关 READY 和心跳测试，不发送消息
│       ├── live_sessions.py    # 真实 API 跨进程断开续聊测试
│       ├── live_mcp.py         # 真实 stdio MCP 与主 LLM 的工具调用测试
│       └── live_mcp_http.py    # HTTP MCP 鉴权、工具调用及会话关闭测试
├── sessions/                   # 运行时会话记录，按创建日期和 UUIDv7 存放
├── .memory/                    # 长期记忆目录，每条记忆保存为独立 Markdown 文件
├── logs/                       # 运行时日志目录，每次启动生成独立日志文件
├── pyproject.toml               # 项目元数据和 Python 依赖配置
├── README.md                    # 项目说明文档
└── AGENTS.md                    # 项目开发与协作约定
```

## 开始

```powershell
# 第一个终端：启动常驻程序。
python -m src

# 第二个终端：连接常驻程序并聊天。
python -m src.terminal
```

默认监听 `ws://127.0.0.1:8765`，在 `.channels.yaml` 中配置；客户端可用 `--url` 指定地址，常驻端可用 `--config` 指定配置文件。`/q` 只断开终端，常驻程序通过其所在终端的 Ctrl+C 停止。

QQ 好友私聊通道也由同一个常驻程序启动，在本地 `.env` 配置 `QQ_APP_ID` 和 `QQ_APP_SECRET`；`.channels.yaml` 只保存环境变量名，不保存密钥。不使用 QQ 时将 `qq_main.enabled` 设为 `false`。QQ 使用共享当前会话，支持普通文本、`/new`、`/resume` 等命令；工具过程不逐条发送，助手回答在本轮结束后合并回复。审批用消息中的 `/approve <审批ID> allow|deny` 答复。

只验证 QQ 连接：`.venv\Scripts\python.exe -B -X utf8 tests/integration/live_qq.py`。测试读取本地配置，确认鉴权、网关 READY 与心跳 ACK，成功或失败都会关闭连接并删除临时日志；不调用模型或发送 QQ 消息。

本地回归测试：`.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests/unit -v`。真实端到端测试：`.venv\Scripts\python.exe -B -X utf8 tests/integration/live_channels.py`，使用现有模型配置，成功或失败均清理本次临时文件和子进程。设计与验证边界见 [多通道设计](docs/多通道设计.md)。
