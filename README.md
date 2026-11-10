# MiniAgent
这是一个学习项目，从零搭建1个agent。模仿codex，claude code，openclaw为主，主要学的是思想，harness工程。

## 项目结构

```text
miniAgent/
├── src/                        # Python 源码包，各子目录通过 __init__.py 标记为包
│   ├── __main__.py             # python -m src 启动入口
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
│   │   └── session_store.py    # 会话 JSONL 存储、恢复与激活
│   ├── ui/
│   │   └── terminal_ui.py      # 终端输入、菜单和历史展示
│   └── common/
│       └── logging_config.py   # 文件诊断日志配置
├── .mcp.json                   # MCP 服务器启动配置，默认不连接任何服务器
├── skills/                     # 本地技能目录
│   └── code-review/
│       └── SKILL.md            # 代码审查技能的元数据和完整说明
├── docs/                       # 项目设计与学习记录文档
│   ├── 持久化设计.md            # 会话 JSONL 持久化格式与事件顺序设计
│   ├── 终端运行界面设计.md      # 自动恢复、命令菜单和历史展示约定
│   ├── 记忆压缩设计.md          # 上下文压缩流程、归档和恢复限制
│   ├── 长期记忆设计.md          # 稳定信息的提取、存储和按需召回
│   ├── mcp接入设计.md           # MCP 接入准备与待实现范围
│   └── 记录.md                  # 项目学习记录
├── tests/                      # 测试目录，子目录通过 __init__.py 支持递归发现
│   ├── unit/
│   │   └── test_sessions.py    # 本地 SDK、工具循环与加载恢复测试
│   └── integration/
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
python -m src
```