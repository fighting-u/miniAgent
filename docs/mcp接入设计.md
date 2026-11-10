# MCP 集成设计

## 基本说明

支持 stdio 和 Streamable HTTP，将 MCP 工具与本地工具一起提供给 Agent。
暂不支持旧版 HTTP+SSE、OAuth 自动登录、Resources 和 Prompts。

## 配置示例

在项目根目录 `.mcp.json` 中配置。文件不存在或 `mcpServers` 为空时，只使用本地工具。

stdio：启动本地 MCP 服务进程，命令与参数分开填写。

```json
{
  "mcpServers": {
    "local_mcp_server": {
      "command": "python",
      "args": ["D:/mcp/server.py"],
      "env": {"API_KEY": "YOUR_KEY"}
    }
  }
}
```

Streamable HTTP：连接已启动的 MCP 服务，`headers` 可省略。

```json
{
  "mcpServers": {
    "remote_mcp_server": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {"Authorization": "Bearer YOUR_TOKEN"}
    }
  }
}
```

两类服务器可同时配置。不写 `type` 时，有 `url` 按 HTTP 连接，否则按 stdio 启动。配置值不展开环境变量占位符。

## 后续补充，需要考虑的问题

- 既然mcp会涉及到远程连接，就会涉及到网络延迟和超时失败的情况，怎么处理？
- 权限的考虑
- mcp的动态发现，动态更新，目前其实是写死的初始化集成；

## 其他细节

- **初始化**：启动后立即后台连接并获取工具列表，与终端输入并行。第一次请求主 LLM 前 `await` 初始化完成，再由 `assemble_tools` 组装完整工具列表；辅助 LLM 无需等待。后续对话复用连接，不动态刷新。
- **命名与调用**：工具名为 `mcp__服务器名__原工具名`，内部映射到对应连接和原名。服务器名和工具名统一通过修正方法处理，仅保留英文字母、数字、下划线和短横线，其余字符逐个替换为 `_`；完整工具名最长 64 字符，超长截断。同名服务器（包括修正后重名）只取配置第一项。不同服务器的同名工具可以共存；完整工具名冲突仍记录并跳过该服务器的工具。调用统一经过现有 Hook。
- **结果**：保留文本、结构化结果和错误标记，序列化后通过 `function_call_output` 返回，保持原 `call_id`。暂不处理图片等多模态结果。
- **超时与兜底**：HTTP 建连、初始化与工具发现阶段、单次 MCP 请求均限时 30 秒。HTTP 首次初始化最多尝试 3 次（含首次），每次失败先关闭本次连接，全部失败则记录并跳过该服务器；不影响其他工具。工具发布后不自动重连，工具调用不自动重试。
- **退出清理**：正常退出或 Ctrl+C 时，统一由 `mcp.close` 收尾初始化及连接任务。stdio 回收子进程；HTTP 关闭会话及客户端，不终止外部服务。

## 涉及文件以及各自的职责

| 文件 | 职责 |
| --- | --- |
| `.mcp.json` | 配置服务器启动参数或 HTTP 地址、请求头。 |
| `src/tools/mcp_client.py` | 读取配置，管理连接，发现工具，维护路由并执行调用。 |
| `src/tools/tools.py` | `initialize_tools` 等待发现完成，`assemble_tools` 合并本地与 MCP 工具。 |
| `src/agent/core.py` | 后台启动初始化，主模型请求前等待工具就绪，分发调用并在退出时清理。 |
| `pyproject.toml` | 声明 MCP SDK 依赖。 |
| `README.md` | 项目文件结构与配置说明。 |
| `tests/integration/live_mcp.py` | 真实 stdio 服务与 LLM 单次工具调用测试。 |
| `tests/integration/live_mcp_http.py` | 真实 HTTP 服务测试，额外验证鉴权请求头和会话关闭。 |

## 测试说明

```powershell
.venv\Scripts\python.exe -B -X utf8 tests/integration/live_mcp.py
.venv\Scripts\python.exe -B -X utf8 tests/integration/live_mcp_http.py
```

测试启动临时 MCP 服务，复用实际 Agent 循环，验证工具发现、一次工具调用及最终回答。每项最多两次主模型请求，不调用辅助模型或触发压缩；成功或失败均清理临时进程和文件，保留测试脚本。

2026-11-10 此前两项真实测试均通过（本次命名修正与连接重试改动未重新运行测试）。HTTP 额外验证了鉴权请求头、关闭会话的 DELETE，以及客户端关闭后外部服务仍在运行。远程 HTTPS、代理和 OAuth 未实测。
