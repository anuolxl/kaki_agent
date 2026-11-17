# App 层契约（v0.2）

实现：`src/kaki/desktop.py`、`app_server.py`、`app_controller.py`、`ui/`。本模块只为本机窗口服务，配置沿用内核，未添加第二套 apiKey/model 配置。

## 生命周期与并发

`desktop.launch(cwd, config_path, resume, model, browser=False, no_open=False, port=0)` 返回进程退出码。macOS 默认原生窗口；`--browser` 用浏览器；`--no-open` 仅监听本机服务。`kaki-agent` 默认调用 launch；`kaki-agent tui` 保留 Textual。Ctrl+C=退出进程130；正常退出=0。原生窗口红色关闭按钮、菜单退出、App 的退出按钮都关闭 Runtime、MCP、数据库和本机服务。浏览器标签关闭仅断开视图，必须点“退出 App”或终端 Ctrl+C 才结束后台。

单实例用 `KAKI_HOME/app/instance.lock` 的 flock；`instance.json`（0600）含 schema_version=1、pid、origin、token。第二次启动通过私有 Bearer 请求已有实例 focus，可打开/切换指定项目。只复用 127.0.0.1 origin，失败不绕过锁。正常退出删除自己的实例记录；锁释放后可以重启。

原生 Cocoa 必须主线程运行，后台 `kaki-app-runtime` 线程拥有 asyncio loop。启动阶段 signal wakeup pipe 由 watcher 读取 SIGINT/SIGTERM；进入 Cocoa 后，使用 MachSignals 并在 run loop 回调中覆盖 pywebview 默认 SIGINT→terminate_ 行为，向 asyncio 投递 stop_event。原生 closing 在 done 前返回 False，阻止 NSApp 提前退出；后台完成后 monitor 才关闭窗口。NativeService 的 ready/done 负责启动和关闭握手。原始事实由 Runtime 恢复规则兜底，不承诺操作系统强杀时每一条增量都保存。

`AppController.engines[workspace_id]` 每个项目一个 Engine、一个 Runtime，最多一个执行任务。open_workspace 由 Lock 串行创建；send 在数据库异步查询后再次校验 busy，防止并发请求绕过。查看/重命名/新建其他会话不切换正在执行的 Runtime；只有新任务 `_execute` 在无其他活跃任务时 switch_session。浏览归档只读，恢复后可继续。

## 关键数据结构

| 对象 / 字段 | 类型与默认 | 语义和构造点 |
|---|---|---|
| Engine.runtime | Runtime，必填 | open_workspace 创建，生命周期归 Controller |
| task | asyncio.Task/null，null | send 创建 `_execute`；finally 清空；busy=存在且未 done |
| session | str，空 | 当前执行/最后执行会话；不是窗口当前查看会话 |
| phase | str，idle | 内存投影，完整枚举见下表；不写 Turn 状态列 |
| started/turn_id | float/null、str空 | 任务起始 Unix 秒 / Runtime 发出的真实 turn_id |
| draft | dict[draft_id,str]，空 | 合并增量，每次推送完整替代文本；不是已提交消息 |
| tools/warnings | dict、list，空 | 当前轮工具进度与实际警告 |
| pending | str，空 | Turn 接受前暂存用户输入，turn.started 后清空避免重复展示 |
| result | dict/null | 已结束结果或准备阶段失败说明；最终持久结果由 Store 提供 |
| notification | TimerHandle/null | 增量60ms合并推送，close 时取消 |
| app-workspaces.json | schema_version=1，paths:list[str] | 最近50个规范项目路径，去重、原子写入0600；不是会话正文 |

`Engine.live()` 返回 workspace_id、session_id、busy、phase、label、started_at、turn_id、drafts、tools、warnings、result、pending_text、question。question 只含 id/question/options，不暴露内部 Future。数据来自内存，同一字段没有跨进程恢复保证。

`detail(workspace,session)` 返回 session（Store 字段含 archived）、messages（id/turn_id/role/text/html/created_at）、turns（id/status/text/stop_reason/error/created/ended/result/tools）、live、model、context、seq。messages 仅展示有正文的 user/assistant；工具记录单独展示。tools 含 tool_call_id/name/state/arguments/preview，preview 最多1600字符；原结果仍持久保存。tool_call_id 用于历史与 live 卡片去重，工具开始/结束时重读持久结果，结束增量保留开始时的参数。此版本历史一次全量返回，尚无分页。

## 状态判定

| phase | 来源 | 是否允许新任务 |
|---|---|---|
| idle | 打开项目且无任务 | 是 |
| preparing | MCP准备、基线快照 | 否，可停止 |
| running/model/tool/compacting | Runtime/Loop 事件 | 否，可停止 |
| waiting_user | ask_user / question.requested | 仅回答当前 question_id，可停止 |
| finishing | 文本完整提交、结束事件、after快照 | 否，必须等待 Task 返回 |
| stopping | 用户停止后等待 Runtime 收尾 | 否，不重复停止 |
| completed/cancelled/failed/interrupted | Task 返回、准备异常或恢复事实 | 是，归档除外 |
| unknown | WebSocket断开 | 否；重连查询真实历史与 live |
| archived/closing/closed | 软归档 / App正在退出 / 退出连接已关闭 | 否 |

`model.committed` 和 `turn.finished` 均不直接让 App 显示 completed，`_execute` 等 `Runtime.run` 返回才解除 busy。同项目另一会话运行时，当前会话可阅读但禁发新任务，并可跳转到正在运行会话。Engine 遗留状态在新任务时重置，Store 原文不重置。

停止本轮：`Controller.stop` → 若 Runtime 已有 active Task，调用 Runtime.cancel 并等待其正常收尾；若仍在 MCP/会话准备，取消 Controller 所拥有的 Task。App 窗口保持可用。退出 App：`Controller.close` → Runtime.close 先标记 closed 再取消，省略新的 after 快照以避免退出时再做耗时全仓操作，并记录警告；已发生的文件副作用不会自动撤回。

## 接口与校验

以下路径都在同一随机 origin 上。`W=/api/workspaces/{workspace}/sessions`；会话 S=`W/{session}`。

| HTTP | 路径 | 请求 | 返回/效果 |
|---|---|---|---|
| GET | /health | 无 | app/version/closing，无路径、凭据 |
| GET | /api/state | 无 | projects/initial/initial_error/version/model/closing/seq |
| POST | /api/workspaces | path:str | workspace_id/path/session_id；Git规范根，禁止空/超长路径 |
| GET | W | archived=0/1 | sessions；默认正常，仅1取归档 |
| POST | W | title可选 | session_id；默认“新会话” |
| GET | S | 无 | detail |
| PATCH | S | title 或 archived:bool | ok；标题校验1–80字符，归档必须非活跃 |
| POST | S/send | text，request_id可选 | 202 accepted；1–100000字符、拒绝竞争和归档；请求ID沿用 Runtime 幂等 |
| POST | S/stop | 空对象 | ok；只允许当前活跃会话 |
| POST | S/answer | question_id/text | ok；会话、问题和活跃状态必须匹配 |
| POST | /api/shutdown | 空对象 | ok；发布 closing，再触发进程停止 |
| POST | /api/focus | path可选 | ok；原生窗口显示/切换工作区 |
| GET/WS | /api/events | 同源握手 | connected / workspace.changed / projects.changed / navigate / resync / app.closing / app.closed |

WS 每条含递增 seq。workspace.changed 含 workspace_id/live/reload，reload 表示 GET detail 核对持久事实；projects.changed 重读项目和会话。队列每客户端256项，满时清队列发 resync，前端重读权威状态。写入超时5秒，heartbeat20秒；关闭时取消并 await writer，不卡住进程。增量草稿是完整投影替换，不能当新的片段再次 append。断线重连1.5秒，在 connected 后重读 detail，不把断线当成功。

仅监听127.0.0.1；严格校验 Host 和 Origin。随机32字节 capability 的 /connect/<ticket> 设置 HttpOnly、SameSite=Strict Cookie 后303跳到根页。Cookie写接口必须同源且 X-Kaki-Client:1；启动器复用用私有 Bearer。无外部脚本、无任意文件静态服务、无跨域开放；Content Security Policy、no-store、no-referrer 默认开启。模型 Key 不进入接口 DTO、日志或前端。Markdown 禁 HTML、远端图片不自动加载，链接仅允许 HTTP/HTTPS 新窗口。

## 兼容与回归落点

sessions.archived 持久字段、v1备份迁移和事务语义见 [core.md](core.md)。App打开历史不启动MCP，首次任务惰性连接；配置和原始会话模型一致性检查不变。打开时在可获得租约的条件下恢复孤儿 Turn；其他进程占有租约时不冒充其任务已完成。数据库迁移保留原表事实，v1备份用于人工回退，不能在v2上直接运行旧版本写入。

tests/test_app_controller.py 覆盖业务状态/持久化；test_app_server.py 覆盖 HTTP/WS/鉴权；test_app_process.py 验证实际子进程退出和单实例；test_tui_pty.py 验证真实终端退出；tests/ui-state.test.mjs 覆盖前端状态。原生 Cocoa 运行由 macOS 手工验收，Linux CI 不声称验证原生窗口。
