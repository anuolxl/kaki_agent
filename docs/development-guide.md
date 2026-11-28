# Kaki v0.4 开发工具与 Skill 操作手册

本期解决的是旧版没有 Git 写操作/受信脚本入口，用户说“开放”也无法改变实际工具表的问题。`kaki_exec` 保留可审查的命令模板；新增 `kaki_git`、`kaki_command`、`kaki_mr` 和能力发现。主 loop、Right Code、上下文压缩、SQLite、TUI/App 公共 Runtime 继续复用。

## 安装和配置

在 Kaki 源码目录同步依赖后，为当前本机安装受信开发能力。下面两个路径都必须替换为你实际的 Harness 和本机已安装的私有 Skill 源；私有 Skill 不进入公共仓库：

```bash
uv sync --extra dev --locked
uv run python scripts/setup_development.py \
  --harness /path/to/harness/teams_spec/be-vcs \
  --private-skill-source /path/to/harness/teams_spec/be-vcs/.agents/skills/vc-casegen-task \
  --python ~/.local/share/uv/tools/kaki-agent/bin/python
kaki-agent skills
kaki-agent tui --help
```

配置仍在 `~/.kaki/config.json`，原来的 apiBase/apiKey/model 不变。新字段和默认值见 [模块契约](module-spec/development.md)。修改后退出旧 App/TUI 再启动，不会热修改正在执行中的 Turn。查看能力可输入“调用 kaki_capabilities，说明当前可用的 Git 工具、命令 profile 和 Skills，只回答”。

## 开发任务示例

```text
$kaki-code-development 修改当前仓库的RTC重复入会回调：code == -999时以-999异常退出。
先执行freshness profile，读取AGENTS和相关module-spec。
fetch origin release/2026-11-30，再创建codex/rtc-minus999分支。
保留已有用户文件，按仓库要求执行make test、review和本地精确文件提交。
先准备MR，不合并或部署。
```

模型可提出以下类型化调用，每一步都有真实工具结果；建分支成功后须重新读取规则，不在同一批次继续编辑：

```json
{"name":"kaki_command","arguments":{"name":"freshness"}}
{"name":"kaki_git","arguments":{"action":"fetch","ref":"release/2026-11-30"}}
{"name":"kaki_git","arguments":{"action":"create_branch","branch":"codex/rtc-minus999","base":"origin/release/2026-11-30"}}
{"name":"kaki_exec","arguments":{"command":"make test ARGS=\"-run TestRTC -count=1\"","timeout":600}}
{"name":"kaki_git","arguments":{"action":"stage","paths":["source/example.go","source/example_test.go"]}}
{"name":"kaki_git","arguments":{"action":"commit","paths":["source/example.go","source/example_test.go"],"message":"fix RTC duplicate join error"}}
```

示例路径和测试名不能原样用于未知仓库。Kaki 会先读文件/规格、确认实际回调和测试入口。`kaki_git diff`返回概览，需要完整patch用已有的 `kaki_exec git diff`。保护分支/用户暂存区冲突会拒绝，不自行stash/reset。仓库的测试代码和Git hooks仍可运行任意进程，当前没有OS沙箱。

## MR 与测试平台

`$kaki-prepare-mr` 先生成含真实差异和验证证据的本地产物。远端 push 与 create_draft 分别需当前具体操作授权；find优先复用已有MR；不确定创建结果保留回执并只查询。工具不提供merge、force或部署动作。

`$vc-casegen-task` 用于中心化集成测试。先读接口/材料/恢复契约，使用 `kaki_artifact_write` 保存真实请求，再通过 casegen-materials 上传原文、casegen-validate 本地校验、casegen-submit 保存回执后创建、casegen-query 查询同一任务。材料上传和创建属于远端写操作，缺少真实环境、泳道、unit、triggerUser或PTD链接时先完成本地准备并询问缺项。`needDelploy` 默认true，可能包含部署；不能把“准备Skill”理解为“执行远端测试”。

## 如何选对工具和 Skill

1. 先判断任务阶段：代码实现→kaki-code-development；只review→kaki-code-review；准备/查MR→kaki-prepare-mr；提交/查中心化任务→vc-casegen-task。
2. 明确指定时使用 `$精确名称`；未指定时，原文与有限上下文改写参与BM25/RRF召回，注入少数正文，其余保留metadata，可用kaki_skill_search继续找。
3. 检查ready与缺少的工具/profile。已安装不等于可执行，可执行不等于有认证或当前授权。用kaki_capabilities核对，不能口头“开放”不存在的工具。
4. 阅读触发/反例与引用，按需使用kaki_tool_search加载MR或浏览器Schema。每Turn重置加载状态；不会把全部MCP工具和Skill正文塞进窗口。
5. 所有实际操作仍经Schema、路径/分支、权限、状态hash、快照/日志的公共链路。检索分数不是正确性证明，复杂歧义继续询问。

例如“查上次harnessTaskID的结果”只走query，不能重新submit；“解释RTC回调，不改文件”只读，不因命中开发Skill而获准写入；“基于release新建分支修改并提交”允许本地开发步骤，MR合并和部署仍有独立边界。

## 日志与源码阅读顺序

`development.py`看类型化Git/命令/MR预检；`tools.py`看注册、延迟发现、执行前复核；`permissions.py`和`decisions.py`看实际人类授权；`context/knowledge.py`看Skill检索与依赖；`loop.py`看分支切换后刷新；`execution.py`看进程取消；`store.py`看unknown持久化。项目SQLite的tool_runs/decisions/events记录调用、授权与终态；MR回执在项目state/workflows/mr。完整字段见模块契约。
