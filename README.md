# Kaki Agent

Python 3.12 本地编码 Agent。默认打开简易桌面 App：工作区与会话侧栏、多轮对话、流式 Markdown、明确的运行/收尾/完成/停止/失败状态。通过 Right Code 的 OpenAI Responses 兼容接口调用模型；保留文件与 Shell 工具、Playwright MCP、本地 Skills、会话持久化、上下文规划/归档、项目记忆和独立 Git 快照。Textual TUI 仍可显式启动。

v0.3 增加独立、可切换策略的意图识别与 Query 改写（默认 hybrid、共享最多一次额外模型尝试、10秒超时回退），以及两种界面共用的任务权限检查与澄清/授权交互。详细流程、源码、配置、验证和边界见 [任务理解与权限指南](docs/understanding-guide.md)及[模块契约](docs/module-spec/understanding.md)。升级前退出旧 App/TUI；数据库自动备份后迁移至 schema3。

当前操作手册与截图：[docs/app-guide.md](docs/app-guide.md)。架构：[docs/architecture.md](docs/architecture.md)。数据与接口契约：[core.md](docs/module-spec/core.md)、[app.md](docs/module-spec/app.md)。[v0.1 内核详解](docs/kaki-agent-guide.md)保留历史源代码摘录。

从源码学习：[SQLite、源码阅读顺序与一次请求全链路](docs/source-reading-guide.md)，分别按顺序讲TUI与App的启动、接单和结果交互，公共Runtime/模型/工具链只展开一次，附函数调用流程图。

理解核心概念与字段：[数据结构与关系指南](docs/data-structures-guide.md)，按工作区、会话、Turn、上下文、模型/工具和数据库梳理真实类型，附状态图、持有关系图与协议数据流。

## 安装与本机部署

需要 Git、uv、ripgrep、Node.js/npm，以及 Google Chrome（浏览器 MCP 使用独立 profile）。本机v0.3的固定部署源码为 `/Users/bytedance/github/kaki_agent-v0.3`；原 `/Users/bytedance/github/kaki_agent` 工作区保留用户未提交文档，不强制改写。全局 `kaki-agent`/`kaki-app` 指向新目录。

```bash
cd /Users/bytedance/github/kaki_agent-v0.3
uv python install 3.12
uv sync --python 3.12 --extra dev --locked
mkdir -p "$HOME/.kaki"
uv export --locked --no-dev --no-emit-project --no-hashes --format requirements.txt --output-file "$HOME/.kaki/runtime-constraints.txt"
uv tool install --editable /Users/bytedance/github/kaki_agent-v0.3 --python 3.12 --force --constraints "$HOME/.kaki/runtime-constraints.txt"
uv run python scripts/install_macos_app.py
open "$HOME/Applications/Kaki.app"
```

将 `~/.local/bin` 加入当前 Shell 的 PATH；本机已有此设置。不要把凭据写入仓库。

```bash
export PATH="$HOME/.local/bin:$PATH"
kaki-agent --version
kaki-agent setup --import-config /Users/bytedance/.pico/config.json
```

`setup` 仅复制兼容的 OpenAI 配置及模型参数，生成 Kaki 自己的 `~/.kaki/config.json`（0600），不会修改旧配置或显示密钥。没有旧配置时运行 `kaki-agent setup`，以隐藏输入填写 Right Code Key。

## 从任意目录启动

```bash
kaki-agent -C /Users/bytedance/go/src/code.byted.org/larkvc/harness/teams_spec/be-vcs/byteview-mp-media_platform

# 指定会话恢复；不会自动重放上次结果未知的命令
kaki-agent -C /path/to/single-git-repo --resume last

# 不安装全局入口时，显式指定 Kaki 的 uv project
uv run --project /Users/bytedance/github/kaki_agent kaki-agent -C /path/to/single-git-repo
```

`-C/--cwd` 指向目标工作区，uv 的项目路径指向 Kaki 自身；二者职责不同。macOS 默认打开原生 WebKit 窗口，也可从 Finder 或 Dock 打开 `~/Applications/Kaki.app` 再选择仓库。再次启动会复用窗口。不要在目标项目中依赖系统同名程序。

```bash
# 等价桌面入口
kaki-app -C /path/to/repo
# 浏览器模式；关标签不会退出后台，请用界面“退出 App”或终端 Ctrl+C
kaki-agent -C /path/to/repo app --browser
# 保留的终端界面
kaki-agent -C /path/to/repo tui
```

运行时显示橙色进度、计时和“停止本轮”；正文结束但快照/持久化仍在执行时显示“正在收尾”；Task 返回后显示绿色“本轮已完成”。失败和停止分别标红/灰，断线显示“状态待确认”。停止本轮保留 App；Ctrl+C、退出按钮或关闭原生窗口退出整个应用进程。Cmd+C 仍用于复制。

```bash
# 参数 -C/--config/--resume 放在子命令之前
kaki-agent -C /path/to/repo doctor
kaki-agent -C /path/to/repo doctor --probe --browser --report /tmp/kaki-doctor.json
kaki-agent -C /path/to/repo sessions
kaki-agent -C /path/to/repo run -m '介绍你自己，不修改文件'
```

`doctor --probe` 会真实调用付费模型；`--browser` 会启动独立浏览器并导航 about:blank。普通 `doctor` 不调用模型。

## TUI

使用 `kaki-agent -C /path/to/repo tui`。Enter 发送；“停止”按钮仅停止本轮；Ctrl+C 退出整个进程（退出码130）；Ctrl+Q 或 `/quit` 正常退出；Ctrl+N 新建会话。

| 命令 | 功能 |
|---|---|
| `/help` | 命令说明 |
| `/new` | 同项目新会话 |
| `/sessions`、`/resume ID` | 列表、恢复会话 |
| `/compact` | 使用私有 Curator 整理窗口，失败有确定性回退 |
| `/context` | 当前/上次窗口的 Token 预算、选择与归档诊断 |
| `/memory` | 项目长期记忆；模型仅在用户明确要求“记住”时写入 |
| `/undo [turn_id]` | 生成 before/after 文件恢复预览；输入 RESTORE 才恢复无冲突文件 |

对 `kaki_ask_user` 的问题，直接在输入框回答。取消不会撤销已经发生的副作用；恢复不会自动重新执行未知操作。窗口宽度建议至少 120 列。

## 配置

优先级：CLI 参数 > 非空 `KAKI_` 嵌套环境变量 > JSON > 默认值。camelCase 与 snake_case 均可；只兼容已实现的字段，不接受未知配置静默失效。

```json
{
  "providers": {"openai": {"apiBase": "https://api.site/v1", "apiKey": "<在本机配置中填写>"}},
  "agents": {"defaults": {"provider": "openai", "model": "openai/gpt-5.6-sol", "maxTokens": 8192, "contextWindowTokens": 65536, "maxToolIterations": 40}},
  "context": {"fastPathThreshold": 0.6, "curatorTimeoutSeconds": 30, "protectFirstN": 3},
  "memory": {"backend": "local", "memoryTopK": 5},
  "runtime": {"checkpoint": {"policy": "interactive"}},
  "tools": {"mcpServers": {"browser": {"type": "stdio", "command": "npx", "args": ["-y", "@playwright/mcp@0.0.85", "--browser", "chrome"], "toolTimeout": 45}}}
}
```

65536 是配置的保守窗口上限，不能证明服务商提供该模型的真实上下文长度。`maxTokens` 显式传为 `max_output_tokens`。同一会话原生 reasoning 项不承诺跨模型迁移；更换模型请新建会话。空环境变量不会覆盖有效配置。

```bash
export KAKI_AGENTS__DEFAULTS__MAX_TOKENS=4096
export KAKI_CONTEXT__CURATOR_TIMEOUT_SECONDS=60
kaki-agent --config /path/to/config.json -C /path/to/repo
```

默认 checkpoint=interactive 在 App/TUI 自动取快照；headless `run` 如需快照设置 policy=always。never 关闭快照。`memory.backend=null` 可关闭项目记忆。退出进程时不再启动新的 after 快照，已确定结果仍落盘；正常停止本轮仍做收尾。

## 本地 Skills

扫描 `~/.kaki/skills/*/SKILL.md`、`项目/.kaki/skills/*/SKILL.md`、`项目/.agents/skills/*/SKILL.md`，同名后者优先。支持 `$技能名`、`/skill 技能名` 显式匹配、中文词项/BM25 召回、always 常驻、版本哈希和目录内引用。

```bash
mkdir -p .kaki/skills/python-review
cat > .kaki/skills/python-review/SKILL.md <<'SKILL'
---
name: python-review
description: Python 修改和 unittest 验证
---
先读相关代码和测试，保持修改范围最小，运行仓库规定的测试命令。
SKILL
```

在 TUI 输入 `$python-review 检查当前改动`。当前TUI开头的 `/skill ...` 会被本地命令解析拦截，请使用 `$技能名 ...`；App可以把 `/skill ...` 作为任务文本匹配。Skills 注入是有预算的；不是技能平台下载或可执行插件系统。

## 状态与恢复

`~/.kaki/projects/<仓库名>-<规范路径hash>/` 保存 `kaki.sqlite3`、`context/archive/`、`context/traces/`、`artifacts/`、`checkpoints/kaki-shadow.git/`、`browser-profile/`、`browser-output/`。完整原始消息始终保留，模型窗口只是其派生选择。移动仓库会改变 workspace_id，需要显式迁移状态目录；不要直接合并两个项目数据库。

独立 shadow Git 不操作用户 `.git/index`、分支或提交。`/undo` 只恢复已确认且当前内容与Git文件模式仍匹配结束快照的文件；它不撤回浏览器/网络副作用，也无法区分本轮期间其他编辑器同时写入的所有权。

## 验证

```bash
uv run python -m compileall -q src
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
node --test tests/ui-state.test.mjs
node --check src/kaki/ui/app.mjs
```

真实 TUI 验证脚本使用真实模型和浏览器，会产生 API 费用。先在新临时目录创建 fixture；脚本拒绝修改业务仓库。图片为实际 Textual 原生界面导出，CJK 仅修复导出字体宽度，不是效果图。

```bash
uv run python scripts/create_validation_fixture.py /tmp/kaki-validation-new/kaki-e2e-workspace
uv run python scripts/validate_live.py --workspace /tmp/kaki-validation-new/kaki-e2e-workspace --output /tmp/kaki-live-evidence
```

当前验收覆盖真实上海秋游/多轮追问、只改 pricing.py 后测试从失败转为通过、before/after 快照、会话重启与显式记忆召回。详细证据与已知限制见技术手册。media_platform 没有为本次 Kaki 验收而修改或运行生产构建；其真实开发测试遵循该仓 AGENTS.md，macOS 使用 `make test`。

## 当前边界

只支持一个 Git 工作区、一个活跃 Turn、Right Code Responses、本地知识与浏览器 MCP。无多 Agent、远程 worker/RPC、跨仓协调、Myna 服务、云端 SkillForge 或完整评测平台。Shell 是宿主机进程，路径检查与少量命令拒绝不是操作系统安全沙箱；仅在可信本地工作区使用。
