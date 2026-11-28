# Kaki v0.3 核心契约

此文档对应当前实现。桌面层契约见 [app.md](app.md)，当前操作指南见 [../app-guide.md](../app-guide.md)，原有内核详解见 [../kaki-agent-guide.md](../kaki-agent-guide.md)。运行语言 Python 3.12，每个 Git 工作区最多单活跃 Turn；边界检查不是 OS 沙箱。

## 配置和兼容

Config 接受 camelCase/snake_case；优先级 CLI > 非空 KAKI_ 双下划线层级环境变量 > JSON > 默认值。未知字段拒绝，extraHeaders/env 的键保留大小写。apiKey 为 SecretStr，只在本机配置中显式写出，保存模式0600。apiBase/Key/model 来自 ~/.kaki/config.json 或显式覆盖，不依赖在 Shell profile 中保存 Key。

默认模型 openai/gpt-5.6-sol，maxTokens=8192，contextWindowTokens=65536，maxToolIterations=40；maxTokens 小于窗口且留输入。窗口值是手动 cap，不是服务商元信息。memory.backend=local/null；MCP 仅 browser；checkpoint.policy=interactive/always/never，interactive 包括 App 和 TUI 自动快照，always 含 headless，never 禁用。

## 类型字段：语义、默认与构造点

| 类型 | 必填与默认 | 语义/构造点 |
|---|---|---|
| Workspace | root/state/id；knowledge_roots/rule_files=[] | root 是 Git 规范根；id 为路径 SHA256 前16位，Workspace.resolve 构造。移动目录改变作用域 |
| Message | role必填，content=''，id=0，session_id/turn_id=''，tool_calls/native_items/parts/artifacts=[]，tool_call_id=None，created_at=UTC | Store._append 赋数据库ID和作用域，payload不含内部ID/时间；native_items保留Responses原生输出顺序，parts是图片引用，artifacts是完整输出引用 |
| ToolCall | id/name/arguments必填 | id是模型 call_id，不等于执行run_id；Provider完整终事件后构造 |
| ToolResult | text必填，failed=False，artifacts/parts/touched_paths=[]，exit_code=None | 工具真实观测；ToolRegistry限制可见Token，原大输出入附件，Store连同Message提交 |
| ModelResponse | text=''，tool_calls/native_items=[]，usage={}，finish_reason='completed'，model/call_id/provider_id='' | Provider.complete构造，call_id是本地调用ID，provider_id是上游response ID；截断工具请求不得执行 |
| TurnResult | turn_id/status/text/stop_reason必填，tools/tool_failures=0，verification=not_run，usage={}，snapshots/error=None | Loop给出执行结果，Runtime补usage和快照后提交终态。completed不等于测试已通过 |
| Event | type必填，turn_id=''，data={} | Runtime.emit发同进程UI事件；部分事件另有Store事务事件，不声称所有UI增量均持久化 |
| TokenBudget | context_length、reserved_output/system/tools/user、margin均必填 | input_limit=window-output-margin；available_history=max(0,input_limit-system-tools-user)，ContextEngine.budget构造 |
| ContextPlan | include_message_ids/archive_refs/drop_message_ids/memory_sections=[]，working_state_injection/notes='' | Curator私有build或确定性策略构造；ID必须存在；不能拆工具组或丢保护组 |
| AssembledContext | messages/budget/estimated_tokens必填，metadata={} | 最后预算门输出；metadata包含path/validation/included_ids/plan/skills/memory/archive来源 |

新增字段需同步序列化、Store、Provider、窗口估算、UI、恢复与本表。SQLite schema=3：v1/v2 自动迁移前通过 SQLite backup API 生成包含 WAL 的一致备份 `kaki.sqlite3.v<原版本>-<随机ID>.backup`（0600），保留/补齐 sessions.archived，新增 decisions 并提交版本3；拒绝更高版本，不静默重置。升级前退出旧进程；回滚二进制必须同时恢复匹配的数据库备份，不能让旧进程写v3。规范完整原文始终在messages表，窗口选择/摘要和任务理解均为派生数据。

## 持久化表和事务边界

Store.run(operation,*args) 进入唯一ThreadPoolExecutor(1)持有的SQLite连接。WAL、synchronous=FULL、foreign_keys=ON。外部 I/O 不放在事务里。

- sessions(id,workspace_id,title,created,updated,archived)：项目内会话；archived INTEGER NOT NULL DEFAULT 0，0=正常/1=软归档，不删除消息、工具结果或快照。Store._new_session 显式写列；_rename_session 标题去首尾空白、1–80字符、拒绝控制字符；_archive_session 拒绝活跃 Turn。AppController 另拦截尚未创建 Turn 的准备阶段。_sessions/_session_summaries 默认仅正常会话，archived=True 仅归档会话。
- turns(id,session_id,request_id UNIQUE,status,text,stop_reason,error,result_json,before_snapshot,after_snapshot,created,ended)：请求幂等与终态。accept_turn 同事务保存用户Message；相同request_id不同内容拒绝；重复终态请求返回旧结果，不再执行。
- messages(id,session_id,turn_id,seq UNIQUE per session,role,content,payload,created)：稳定递增ID、原始领域payload。
- tool_runs(id,turn_id,tool_call_id,name,arguments,state,result_json,started,ended)：commit_model 同事务保存assistant原生项与全部planned；mark_running在执行前提交；commit_tool同事务保存结果与tool消息。已完成结果不会被未完成同批读操作覆盖。
- events(seq,session_id,turn_id,type,payload,created)：事务内关键事实事件。
- model_calls(id,turn_id,kind,model,status,usage,metadata,created,ended)：每次网络尝试，kind=main/curator/finalize/understanding。累计usage含所有已知调用，usage缺失计unknown_calls；会话恢复模型检查只使用main/finalize，understanding不得改变主会话模型约束。
- context_state(session_id,manifest,working,plan,version)：可重建的窗口状态，更新递增版本。
- archives(id,session_id,message_ids,artifact,summary,tags,created)：原文归档，artifact内path/sha256/size；引用限定会话，读取校验哈希。
- memories(id,workspace_id,user_id,content,kind,state,version,source_ids,created,updated)：active/superseded/retracted，替代产生新版本并保留旧行。
- snapshots(id,turn_id,phase,state,commit_id,metadata,created)：before/after，creating/ready/failed；失败不被描述为可恢复。
- decisions(id,session_id,turn_id,kind,state,question,options,operation,response,created,ended)：用户交互事实。kind=clarification/approval/task，state=pending/answered/expired。options/operation/response为JSON。DecisionService构造q_，Store创建时把Turn设为waiting_user；有效回答同事务写response、恢复running；clarification另外追加真实user Message并返回message_id。取消/结束/恢复使pending过期，临时授权不从数据库恢复。

## 状态与恢复

Turn活跃态queued/running/waiting_user；终态completed/interrupted/cancelled/failed不可二次覆盖。工具planned/running/succeeded/failed/denied/cancelled/unknown。denied只允许从planned提交，started=null、executed=false，仍补配对tool消息；拒绝不是执行失败或UNKNOWN。恢复持有workspace lease：planned→cancelled、running→unknown，补明确的失败/未知tool观测以关闭协议组；活跃Turn→interrupted/process_exit；运行中模型调用→interrupted。结果未知的副作用不自动重放。

`Recovery.reconcile`给出最近中断任务、确定结果、未知参数、快照引用和草稿线索；只在用户新任务时注入，不自动继续旧命令。Session恢复要求主模型一致，避免跨模型重放opaque reasoning。

## 模块接口

- Runtime.initialize(resume,connect_browser=True)/ensure_browser/submit(text,request_id)->Task/run(...)->TurnResult/answer(question_id,text)/cancel/close。负责租约、before/after和唯一终态发布；interactive标记控制快照策略。App 用 connect_browser=False 打开历史，首次发送才连接 MCP，普通 TUI 保持原行为。close 先标记 closed 再取消，不在进程退出时启动新的 after 快照，记录明确警告；普通停止本轮仍执行正常收尾。恢复机制仍处理强制退出留下的 UNKNOWN。
- RightCodeProvider.complete(messages,tools,model,max_tokens,turn_id,kind,on_delta,tool_choice,max_attempts=4)->ModelResponse；close。理解调用固定max_attempts=1，外层10秒deadline；主调用保留原有限重试。delta仅展示，完整终事件后才允许工具执行。
- ContextEngine.assemble(session,history,user,tools,force,recovery)->AssembledContext；rebalance(session,window,tools)；after_turn。所有历史选择经闭包/预算门。
- ToolRegistry.prepare/execute/execute_many：先规范化参数、校验Schema/路径和能力、等待必要授权，再校验真实目标及写入前hash、捕获必要before快照、mark_running和实际I/O；safe READ预检串行、执行最多4并发，其他串行；每个完成立即落库。无权限服务默认拒绝。ToolResult新增executed=True、denied=False、decision_id=None；拒绝/预检失败改executed=false，只有实际进入工具实现才为true。测试证据还必须满足executed，不能由拒绝结果生成verification。
- BrowserMcp.start/call/close：专用owner task持有stdio/ClientSession，队列串行，独立项目profile，文本/图片/结构化错误规范化。
- Store.run：内部操作名按源码定义，不虚构RPC/网络服务。
- Checkpoint.capture(turn,phase)/plan_restore(turn)/apply(plan,approved_paths)：只用独立git-dir；恢复前预览/批准，写时再校验blob内容和Git文件模式（普通/可执行/符号链接/目录）。RestorePlan.paths包含current_mode、expected_current_mode，避免覆盖仅权限变化或类型变化。
- Memory.recall/remember/segment；Store.retract用于撤回；Skills.catalog/resolve/read；Archive.save/load/read，精确分页/来源校验。

## Token与知识参数

margin=max(128,window*2%)；fast阈值0.6*available_history；protectFirstN=3个初始用户组；Curator最多12轮/30秒，只开放budget/archive/retrieve/search/memory/relevance/state/build；8/12/20条working state上限；相关度0.95衰减/引用+0.15。Curator失败走保护+最近+相关候选、整组裁剪、最终门，不能靠删当前请求蒙混通过。

tiktoken o200k_base，加载失败用UTF8字节/3保守回退；估算包含完整wire input、tools与原生项，每张图片另计4096。输出上限也发送到上游。旧工具正文省略保留最近3条，原文和artifact不变；循环中必要时移除旧完整模型-工具exchange。最新输入或保护内容本身仍超限时明确失败。Provider上下文溢出最多应急2次，不计入正常迭代预算；空输出恢复次数有限。

## 验证与兼容样例

自动化覆盖配置别名/优先级/密钥诊断、路径逃逸、执行租约、协议配对、崩溃UNKNOWN、幂等与终态、真实文件/Shell、取消恢复、ask_user、迭代上限、快慢路径/超时/无效plan/固定开销溢出、原文归档、活动Turn重平衡、本地Skill/中文召回、记忆版本、shadow Git原始字节/冲突/用户index不变、Responses SSE终事件与TUI流式归并。真实模型/TUI验收结果见技术手册，CI未执行时不声称CI通过。

v0.2 另覆盖 v1→v2 备份迁移、会话归档/恢复/重命名、收尾阶段不提前报完成、等待用户作用域、关闭跳过新快照、HTTP/WS 鉴权、慢客户端 resync、真实子进程 SIGINT 130/单实例、PTY `/quit` 和 Ctrl+C 退出，以及 Node 状态决策测试。

## v0.3 任务理解、权限与交互契约

详见[任务理解模块契约](understanding.md)和[源码与操作指南](../understanding-guide.md)。新增UnderstandingService、PermissionService、DecisionService，复用同一个Runtime；TUI/App仅呈现不同。

`Runtime._turn`在accept_turn后执行理解；默认有关键歧义才询问，最多两次预处理澄清，同一Turn，澄清不重置额外模型预算。`model_decide`把未解引用交主loop决定，但不放宽权限。无交互CLI需要人类输入时返回interrupted/clarification_required或approval_required，CLI退出2；不会创建无限等待Future。

快照时机改为首次通过权限的workspace.write或test.execute之前；普通只读对话不建立before/after。测试可产生文件副作用，因此也捕获before。只有存在before才捕获after；关闭程序仍跳过新after。快照失败单独警告，不伪造可恢复性。

权限默认task_scoped：明确的当前人类修改/测试任务可自动获得相应范围；只回答、禁止写入、排除路径及本地read_only配置优先。模型意图、改写、网页、Skill、旧记忆不授权。权限不明确时默认询问；授权once/turn仅当前工作区/会话/Turn，文件范围按目标规范路径，测试按cwd+argv，浏览器交互按完整操作指纹。硬拒绝凭据、Agent配置/指令/Skills控制文件、直接操作Git内部元数据、部署、管理员命令及浏览器任意JS/安装/上传。v0.4已注册的类型化Git操作另按development契约检查，不属于任意Shell放行。

kaki_exec以argv启动，不再使用bash -c；只支持源码列出的pwd/echo、只读Git、测试和格式化模板，其他命令/管道/替换/重定向拒绝。kaki_command执行预先注册并校验的命名profile，不由模型构造任意Shell。Git禁外部diff/textconv/pager，禁可选index更新，统一排除凭据和Agent配置路径。测试子进程使用当前用户权限，没有OS沙箱，不能保证其内部文件/网络隔离。

新测试覆盖9种独立策略组合、单网络尝试、超时/取消、无效来源/约束丢失、原文与Turn保持、模型意图不授权、预检拒绝与配对、授权重试与作用域、等待中取消和并发文件变化、schema3备份迁移、TUI/App实际决策链。

测试runner不改变：uv run前缀必须传给真实子进程；权限分类根据后面的受支持测试入口判断。它仍是test.execute，需要任务范围或批准，不能通过uv把任意命令伪装成只读。


## v0.4 开发能力补充

开发工具、受信命令、Skill依赖和发现、授权与分支切换后上下文刷新，以[development模块契约](development.md)为准。 新增字段均提供向后兼容默认值，SQLite仍为schema3；旧版关于“不支持Git写操作”的限制仅适用于旧版和kaki_exec模板，不再代表kaki_git/命名profile。

长保护Turn在恢复/组装时可能包含超过窗口的大量工具观察。build_plan在压力下保留原始用户约束和完整调用/结果配对，将较早工具正文投影为可回查占位符，保留最近3个工具正文，再按完整Turn裁剪。新增context plan元数据`elided_tool_ids=[]`记录实际保留ID但正文分页的tool消息；assemble将它们的原文与遗漏消息一起归档并写manifest.archive_ref，SQLite消息不改变。默认空列表兼容旧plan。当前用户/规则/Schema本身超限仍明确失败，不能靠裁剪用户约束假装成功。test_resume_large_protected_tool_turn_pages_observations_without_losing_facts覆盖Curator正常、无效与超时三条路径。

进一步压力来自Responses原生reasoning.encrypted_content，不能只省略工具正文。若仍超限，宿主按完整closed assistant+全部tool-result exchange分页，连同原生项整体归档，保护的初始用户文本与独立assistant最终回答保持原样；最新完整exchange保留，存活exchange的native items逐字不变。新增plan元数据paged_exchange_ids=[]，assemble与active rebalance均使用该策略。单条当前输入/规则/最新exchange本身超限仍失败，绝不拆掉半个工具组或伪造reasoning。

Turn终态的tools/tool_failures/verification统一从已提交tool_runs重建。后续模型ReadTimeout、取消或总结失败不能将前面真实工具数清零或抹掉已经完成的测试；status仍保留failed/cancelled，不把测试通过误当整项任务完成。verification_evidence由loop即时事件与Runtime终态共同消费；没有执行的授权拒绝不算测试验证，external.write平台提交不算测试。test_provider_failure_retains_committed_tool_and_test_evidence覆盖测试已完成后模型超时的持久化结果。


## v0.4.2 原生reasoning预算与执行事实

Message新增派生字段native_token_reserve=None，不写入canonical payload；Store.commit_model按接口completion_tokens与reasoning_tokens的较大值构造，历史通过model.committed事件与model_calls用量关联投影，原始messages JSON不改写。Curator临时消息同样记预留值。该值是整次输出token上界，为不可见reasoning保守预留；estimate仅在估算副本中移除对应encrypted_content并加入reserve，实际wire_input和原生项逐字不变。没有可信正用量的旧记录仍按原始wire估算。SQLite保持schema3，无需改表；归档包含派生字段以便审计。tests/test_context.py覆盖密文长度与token用量的区别、wire不变、历史只读关联与原payload保持。下游网关若不准确报告用量，仍可能估算偏差，Provider上下文溢出恢复和最终预算门继续有效。

依据：OpenAI Responses reasoning指南说明reasoning tokens计入输出用量，encrypted_content为可回传的加密状态（https://developers.openai.com/api/docs/guides/reasoning）。本机实际Right Code记录包含reasoning_tokens=10/20/38等，不把密文字节的BPE长度当作真实推理用量。

含原生reasoning的窗口在压力下只移除完整closed exchange，不改写保留的旧工具正文；这样保留项的内容与顺序保持原样，避免修改历史前缀导致上游丢弃reasoning而反复读取。纯文本窗口仍可使用旧工具正文占位策略。此规则同时作用assemble和active rebalance。官方前缀保持指南说明修改旧工具输出可能移除reasoning，删除完整项与追加新项本身不构成相同原因（具体网关仍以实际行为为准）。

快照每次capture重建Kaki自身shadow Git index，再按当前排除规则add；旧版本已进入shadow索引的.worktrees Gitlink不会因新ignore规则而残留。只作用于state/checkpoints影子仓，用户Git index/HEAD/已有工作树文件不变；test_capture_rebuilds_shadow_index_after_new_nested_repo_exclusion验证升级场景。
