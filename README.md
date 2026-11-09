# MiniAgent
这是一个学习项目，从零搭建1个agent。模仿codex，claude code，openclaw为主，主要学的是思想，harness工程。

## 项目结构

```text
miniAgent/
├── core.py                     # Agent 主入口，负责对话循环、模型请求和工具调度
├── session_store.py            # 会话 JSONL 的创建、追加、加载与中断工具结果处理
├── terminal_ui.py              # 终端输入、斜杠菜单、会话选择与历史展示
├── context_compactor.py        # 上下文压缩管线，负责工具结果落盘、历史裁剪和摘要
├── logging_config.py           # 配置仅写入文件的诊断日志
├── tools.py                    # 工具定义、工具处理函数和任务计划管理
├── hook.py                     # Hook 注册与触发，包含权限检查和运行日志扩展
├── memory.py                   # 负责长期记忆的提取、文件存储和按需召回
├── system_prompt.py            # 构建并导出发送给模型的系统提示词
├── skill.py                    # 发现、列出并按需加载本地技能
├── skills/                     # 本地技能目录
│   └── code-review/
│       └── SKILL.md            # 代码审查技能的元数据和完整说明
├── docs/                       # 项目设计与学习记录文档
│   ├── 持久化设计.md            # 会话 JSONL 持久化格式与事件顺序设计
│   ├── 终端运行界面设计.md      # 自动恢复、命令菜单和历史展示约定
│   └── 记录.md                  # 项目学习记录
├── tests/                      # 持久化专项测试
│   ├── test_sessions.py        # 本地 SDK、工具循环与加载恢复测试
│   └── live_sessions.py        # 真实 API 跨进程断开续聊测试
├── sessions/                   # 运行时会话记录，按创建日期和 UUIDv7 存放
├── .memory/                    # 长期记忆目录，每条记忆保存为独立 Markdown 文件
├── logs/                       # 运行时日志目录，每次启动生成独立日志文件
├── pyproject.toml               # 项目元数据和 Python 依赖配置
├── README.md                    # 项目说明文档
└── AGENTS.md                    # 项目开发与协作约定
```

## 会话持久化设计

格式约定见 [持久化设计](docs/持久化设计.md)，已实现消息追加与加载续聊，`compact` 记录尚未实现：

- 每个会话保存为 `sessions/YYYY/MM/DD/<session-id>.jsonl`，会话标识使用 UUIDv7。
- 外层 `type` 使用 `messages` 或 `compact`，通过 `ordinal` 和 `turn_id` 标识记录顺序与用户请求。
- `payload` 保留 API 原始类型名，`payload.metadata` 补充 `api_format`；模型的 `message`、`reasoning` 另记录 `model_name`。
- 省略 payload 中值为 `None` 的字段；恢复 API 输入时移除附加的 `metadata`。

启动终端（PowerShell）：

```powershell
.venv\Scripts\python.exe core.py
```

启动时自动加载最近写入的会话并展示历史；没有会话时自动创建。输入 `/` 展开中文命令菜单：`/new` 新建、`/resume` 选择历史会话、`/q` 退出。会话列表仅展示选择序号、最近时间和截短的首条用户问题，不显示会话 ID。

工具调用缺少已保存结果时，恢复会补充“结果未确认”的说明，不自动重复执行工具。当前恢复范围为未压缩会话。界面约定见 [终端运行界面设计](docs/终端运行界面设计.md)。
