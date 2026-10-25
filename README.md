# MiniAgent
这是一个学习项目，从零搭建1个agent。模仿codex，claude code，openclaw为主，主要学的是思想，harness工程。

## 项目结构

```text
miniAgent/
├── core.py                     # Agent 主入口，负责对话循环、模型请求和工具调度
├── tools.py                    # 工具定义、工具处理函数和任务计划管理
├── hook.py                     # Hook 注册与触发，包含权限检查和运行日志扩展
├── system_prompt.py            # 构建并导出发送给模型的系统提示词
├── skill.py                    # 发现、列出并按需加载本地技能
├── skills/                     # 本地技能目录
│   └── code-review/
│       └── SKILL.md            # 代码审查技能的元数据和完整说明
├── pyproject.toml               # 项目元数据和 Python 依赖配置
├── README.md                    # 项目说明文档
└── AGENTS.md                    # 项目开发与协作约定
```
