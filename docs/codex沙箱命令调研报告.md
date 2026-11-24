# Codex 沙箱命令调研报告

调研环境为 Windows、PowerShell、Codex CLI 0.167.0，日期为 2026-11-24。

**结论**：miniAgent 可以用 `codex sandbox` 直接运行已有的 PowerShell 命令，无需发起模型请求。本机已验证“允许读取普通文件，只给指定项目目录写权限”的用法。若要求“只有 `allowed_dirs` 可读写，名单外全部禁止”，当前 Windows `elevated` 后端无法直接执行我们测试的读取白名单策略。

## 1. 基本示例-一条命令如何读

```powershell
codex sandbox -P ':workspace' -C 'D:\projects\others\miniAgent' -- powershell.exe -NoProfile -NonInteractive -Command 'Get-Location'
```

| 片段 | 用途 |
|---|---|
| `codex sandbox` | 在本机启动命令，按当前平台选择沙箱实现。此版本在 Windows 上直接使用这个入口。 |
| `-P ':workspace'` | 选择内置的工作区权限策略。 |
| `-C <目录>` | 设置策略解析和命令执行使用的工作目录。它会影响工作区规则，但单靠设置起始目录无法限制文件访问。 |
| `--` | 结束 Codex 参数。其后的可执行文件和参数交给子进程。 |
| `powershell.exe ...` | 真正执行的程序。这里禁用 PowerShell 配置脚本和交互提示，并输出工作目录。 |

下文的命令用于说明语法；第 5 节单列本机实际执行的读写探针。查看本机版本和完整帮助可运行 `codex --version`、`codex sandbox --help`。[官方 CLI 参考](https://developers.openai.com/codex/cli/reference/#codex-sandbox)

## 2. “模式”分两层

### 2.1 权限策略决定允许做什么

| `-P` 的值 | 文件权限和典型用途 |
|---|---|
| `:read-only` | 只读检查，例如读取源码、查看配置。只读表示限制写入，所有位置都可读，不仅限于当前工作区。 |
| `:workspace` | 允许修改工作区和系统临时目录；工作区内的 `.git`、`.codex` 等路径有额外保护。适合常规项目编辑。工作区指的是启动 `codex sandbox` 时的工作目录及其子目录，但可以通过-C 显示指定，例如`-P ':workspace' -C 'D:\projects\others\miniAgent'` |
| `:danger-full-access` | 移除 Codex 的本地沙箱限制，命令按操作系统账号本身的权限运行。 |
| 自定义策略，如取名miniagent | 从codex的config.toml配置中的 `permissions.miniagent` 读取规则，可以分别描述文件访问和网络访问。 |

自定义策略的配置，举个例子，在codex的config.toml中添加如下

```toml
[permissions.miniagent.filesystem]
"D:/projects" = "write"
"D:/projects/archive" = "read"
```

具体规则是：自定义路径支持 `read`（只读）、`write`（可读写）、`deny`（禁止访问）。没有“只写不可读”模式。更具体的路径规则优先；同等具体时，优先级为 `deny > write > read`。因此父目录可写时，仍可单独将子目录设为只读。策略能否落地还取决于后端。[官方权限说明](https://developers.openai.com/codex/permissions/)

也可以通过-c （-c = -- config ) 动态传入，比如

```powershell
codex sandbox `
  -c 'permissions.miniagent.filesystem={":root"="read","D:/projects"="write"}' `
  -P miniagent `
  ...
```

其中`":root"="read"`指默认都可读，如果不显示设置，则默认都为deny权限，如果只配置`"D:/projects" = "write"`在elevated后端下，沙箱都无法启动，因为其根目录D:\默认为deny权限，顶层都无法读，所以会报错`requires effective :root read access`。此外，还有`:minimal` 是指授予必要的运行环境读取权限， 是 Codex 定义的一个特殊路径规则，表示允许读取当前操作系统下运行常用工具所需的基础文件和目录。

---

3种后端均支持自定义策略，但是支持配置 != 支持执行，支持程度也不一样。unelevated就不支持deny自定义配置；elevated支持配置目标write，但其实现上限制了其目标更顶层的必须都设为可读；**mxc就支持更多，能满足我想实现的仅配置文件夹可读写，其他默认不可读写。**

比如

```toml
[permissions.miniagent.filesystem]
":minimal" = "read"
"D:/projects" = "write"
"D:/workspace" = "write"
"D:/data" = "write"
[permissions.miniagent.network]
enabled = false
```



### 2.2 执行权限策略的具体后端有多种选择

| 配置 `windows.sandbox` | 执行方式和能力 |
|---|---|
| `unelevated` | 使用当前用户派生的受限令牌和 Windows 访问控制列表（ACL），无需管理员初始化。本机实测能拦截范围外写入。显式拒读路径不受支持，网络隔离也较弱。 |
| `elevated` | 初始化时用管理员权限设置专用低权限账号、文件权限和防火墙规则。日常命令在低权限账号下运行。支持给指定路径设置拒读规则，但对默认禁止读取的策略有额外限制，见第 5 节。 |
| `mxc` | 全称 Microsoft Execution Containers，需要设备具备对应 Windows 能力，且管理策略允许使用。本次没有验证本机 MXC 的可用性或读取边界。 |

不显示指定，默认根据codex配置文件`%USERPROFILE%\.codex\config.toml`里的配置值作为选择，例如

```toml
[windows]
sandbox = "elevated"
```

默认是unelevated，若要改默认为elevated，运行 `codex sandbox setup --elevated --current-user`。

elevated和unelevated均不可实现配置文件夹可读写，其他位置读写均不可，只能保证配置文件夹可写，其他位置可读但不可写。

若要改为mxc需要手动改toml文件，因为26.10微软新出的转为agent提供的沙箱，还不完全成熟。

若只想单次探测 MXC，官方示例为 `codex -c windows.sandbox=mxc sandbox --include-managed-config -P :workspace -- cmd.exe /d /c echo MXC_OK`。输出标记只能验证命令成功启动。[官方 Windows 沙箱说明](https://developers.openai.com/codex/windows/)

## 3. Windows 版参数速查

| 参数 | 含义、用法与约束 |
|---|---|
| `[COMMAND]...` | 子进程的可执行文件及参数，例如 `-- powershell.exe -NoProfile -Command 'Get-Date'`。 |
| `-c, --config <key=value>` | 仅为本次调用覆盖一个配置项，可重复。键支持点号层级，值按 TOML 解析，解析失败则按字符串处理。例如 `-c windows.sandbox=elevated`。 |
| `-P, --permission-profile <NAME>` | 选权限策略，例如 `-P ':read-only'` 或第 4 节的 `-P miniagent`。 |
| `-p, --profile <NAME>` | 加载额外的配置文件 `$CODEX_HOME/<NAME>.config.toml`，叠加到基础用户配置。比如 `-p dev -P miniagent` 会加载 `dev.config.toml`，再选择名为 `miniagent` 的权限策略。注意大小写。 |
| `-C, --cd <DIR>` | 指定本次工作目录，例如 `-C 'D:\projects\others\miniAgent'`。此子命令要求同时使用 `-P`。 |
| `--include-managed-config` | 与 `-P` 配合，将托管要求纳入显式策略解析。直接指定 `-P` 而省略此项时，沙箱辅助命令默认不纳入这些要求。 |
| `--sandbox-state-json <JSON>` | 接收 `codex/sandbox-state` 提供的完整状态 JSON，以其中的工作目录、权限等信息运行命令。不能与 `-P`、`-C` 或 `--include-managed-config` 同用。 |
| `--sandbox-state-readable-root <绝对路径>` | 仅配合上述 JSON，给已有权限**追加**可读目录，可重复。适合补充 Python 安装目录；不会收回原本对其他目录的读取权限。 |
| `--sandbox-state-disable-network` | 仅配合上述 JSON，为这次执行关闭直接网络访问。实际网络隔离能力取决于所用后端。 |
| `--enable <FEATURE>`、`--disable <FEATURE>` | 启用或关闭已知功能开关，可重复。分别等价于 `-c features.<名称>=true` 和 `-c features.<名称>=false`。可用名称可通过 `codex features list` 查看。 |
| `-h, --help` | 查看帮助，`-h` 显示摘要。`codex sandbox help` 也是帮助入口。 |

`codex sandbox uninstall` 是管理子命令，用于移除传统 Windows 沙箱建立的机器级账号与网络规则，需管理员权限。单次命令结束时无需运行它。[官方 CLI 参考](https://developers.openai.com/codex/cli/reference/#codex-sandbox) · [本地参数定义](D:/projects/others/codex/codex-rs/cli/src/lib.rs:29)

