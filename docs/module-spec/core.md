# Kaki v0.1 核心契约

此文档对应当前实现。完整架构与操作指南见 [../kaki-agent-guide.md](../kaki-agent-guide.md)。运行语言 Python 3.12，单 Git 工作区，单活跃 Turn；边界检查不是 OS 沙箱。

## 配置和兼容

Config 接受 camelCase/snake_case；优先级 CLI > 非空 KAKI_ 双下划线层级环境变量 > JSON > 默认值。未知字段拒绝，extraHeaders/env 的键保留大小写。apiKey 为 SecretStr，只在本机配置中显式写出，保存模式0600。apiBase/Key/model 来自 ~/.kaki/config.json 或显式覆盖，不依赖在 Shell profile 中保存 Key。

默认模型 openai/gpt-5.6-sol，maxTokens=8192，contextWindowTokens=65536，maxToolIterations=40；maxTokens 小于窗口且留输入。窗口值是手动 cap，不是服务商元信息。memory.backend=local/null；MCP 仅 browser；checkpoint.policy=interactive/always/never，interactive 仅 TUI 自动快照，always 含 headless，never 禁用。

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

新增字段需同步序列化、Store、Provider、窗口估算、UI、恢复与本表。SQLite schema=1：拒绝更高版本，不静默重置；v0.1尚未承诺对未来版本自动迁移。规范完整原文始终在messages表，窗口选择/摘要均为派生数据。

## 持久化表和事务边界

Store.run(operation,*args) 进入唯一ThreadPoolExecutor(1)持有的SQLite连接。WAL、synchronous=FULL、foreign_keys=ON。外部 I/O 不放在事务里。

- sessions(id,workspace_id,title,created,updated)：项目内会话。
- turns(id,session_id,request_id UNIQUE,status,text,stop_reason,error,result_json,before_snapshot,after_snapshot,created,ended)：请求幂等与终态。accept_turn 同事务保存用户Message；相同request_id不同内容拒绝；重复终态请求返回旧结果，不再执行。
- messages(id,session_id,turn_id,seq UNIQUE per session,role,content,payload,created)：稳定递增ID、原始领域payload。
- tool_runs(id,turn_id,tool_call_id,name,arguments,state,result_json,started,ended)：commit_model 同事务保存assistant原生项与全部planned；mark_running在执行前提交；commit_tool同事务保存结果与tool消息。已完成结果不会被未完成同批读操作覆盖。
- events(seq,session_id,turn_id,type,payload,created)：事务内关键事实事件。
- model_calls(id,turn_id,kind,model,status,usage,metadata,created,ended)：每次网络尝试，kind=main/curator/finalize。累计usage含所有已知调用，usage缺失计unknown_calls。
- context_state(session_id,manifest,working,plan,version)：可重建的窗口状态，更新递增版本。
- archives(id,session_id,message_ids,artifact,summary,tags,created)：原文归档，artifact内path/sha256/size；引用限定会话，读取校验哈希。
- memories(id,workspace_id,user_id,content,kind,state,version,source_ids,created,updated)：active/superseded/retracted，替代产生新版本并保留旧行。
- snapshots(id,turn_id,phase,state,commit_id,metadata,created)：before/after，creating/ready/failed；失败不被描述为可恢复。

## 状态与恢复

Turn活跃态queued/running/waiting_user；终态completed/interrupted/cancelled/failed不可二次覆盖。工具planned/running/succeeded/failed/cancelled/unknown。恢复持有workspace lease：planned→cancelled、running→unknown，补明确的失败/未知tool观测以关闭协议组；活跃Turn→interrupted/process_exit；运行中模型调用→interrupted。结果未知的副作用不自动重放。

`Recovery.reconcile`给出最近中断任务、确定结果、未知参数、快照引用和草稿线索；只在用户新任务时注入，不自动继续旧命令。Session恢复要求主模型一致，避免跨模型重放opaque reasoning。

## 模块接口

- Runtime.initialize(resume)/submit(text,request_id)->Task/run(...)->TurnResult/answer(question_id,text)/cancel/close。负责租约、before/after和唯一终态发布；interactive标记控制快照策略。
- RightCodeProvider.complete(messages,tools,model,max_tokens,turn_id,kind,on_delta,tool_choice)->ModelResponse；close。delta仅展示，完整终事件后才允许工具执行。
- ContextEngine.assemble(session,history,user,tools,force,recovery)->AssembledContext；rebalance(session,window,tools)；after_turn。所有历史选择经闭包/预算门。
- ToolRegistry.execute_many(calls,ctx,on_start,on_complete)->按输入序ToolResult；safe READ最多4，其他串行；每个完成即调用on_complete落库。Schema/路径/超时失败返回failed观测；Cancel向上传播。
- BrowserMcp.start/call/close：专用owner task持有stdio/ClientSession，队列串行，独立项目profile，文本/图片/结构化错误规范化。
- Store.run：内部操作名按源码定义，不虚构RPC/网络服务。
- Checkpoint.capture(turn,phase)/plan_restore(turn)/apply(plan,approved_paths)：只用独立git-dir；恢复前预览/批准，写时再校验blob内容和Git文件模式（普通/可执行/符号链接/目录）。RestorePlan.paths包含current_mode、expected_current_mode，避免覆盖仅权限变化或类型变化。
- Memory.recall/remember/segment；Store.retract用于撤回；Skills.catalog/resolve/read；Archive.save/load/read，精确分页/来源校验。

## Token与知识参数

margin=max(128,window*2%)；fast阈值0.6*available_history；protectFirstN=3个初始用户组；Curator最多12轮/30秒，只开放budget/archive/retrieve/search/memory/relevance/state/build；8/12/20条working state上限；相关度0.95衰减/引用+0.15。Curator失败走保护+最近+相关候选、整组裁剪、最终门，不能靠删当前请求蒙混通过。

tiktoken o200k_base，加载失败用UTF8字节/3保守回退；估算包含完整wire input、tools与原生项，每张图片另计4096。输出上限也发送到上游。旧工具正文省略保留最近3条，原文和artifact不变；循环中必要时移除旧完整模型-工具exchange。最新输入或保护内容本身仍超限时明确失败。Provider上下文溢出最多应急2次，不计入正常迭代预算；空输出恢复次数有限。

## 验证与兼容样例

自动化覆盖配置别名/优先级/密钥诊断、路径逃逸、执行租约、协议配对、崩溃UNKNOWN、幂等与终态、真实文件/Shell、取消恢复、ask_user、迭代上限、快慢路径/超时/无效plan/固定开销溢出、原文归档、活动Turn重平衡、本地Skill/中文召回、记忆版本、shadow Git原始字节/冲突/用户index不变、Responses SSE终事件与TUI流式归并。真实模型/TUI验收结果见技术手册，CI未执行时不声称CI通过。
