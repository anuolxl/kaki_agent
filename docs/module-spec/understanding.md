---
type: reference
audience: seeker
owner: kaki-agent
status: active
---
# 任务理解、权限与人类决策契约（v0.3）

入口形态：本地CLI/TUI/App，共用Runtime。模块源码：`src/kaki/understanding/`、`permissions.py`、`decisions.py`；调用方Runtime、AgentLoop、ContextEngine和ToolRegistry。操作与实现讲解见[指南](../understanding-guide.md)。不引入额外Agent、服务、embedding模型或向量数据库。

## 职责与顺序

1. Store.accept_turn原样保存当前user Message，创建Turn。
2. UnderstandingService.prepare(user,history)先本地分析；必要时通过同一个RightCodeProvider做一次私有联合推理。
3. 校验来源、关键标识符、数字/单位和禁止写入约束；允许intent成功而rewrite单独回退。默认有关键歧义才由DecisionService询问。
4. 用户澄清追加真实user Message，仍属于原Turn；refine仅用本地策略，不重新获得模型调用预算。
5. RequestContext进入ContextEngine：保留原始user，派生提示进入system；检索按原文优先的加权RRF融合。
6. 主loop保存完整assistant及planned工具后，ToolRegistry预检PermissionService；允许后重新校验目标/hash、必要before快照、mark_running、实际I/O、commit_tool。
7. Runtime统一收尾，必要after快照、累计usage、finish_turn、turn.finished。

`/compact`保留手动上下文整理分支，不做任务理解，也不会因只整理窗口而建立文件快照。

## 配置字段

均继承Options：camelCase/snake_case、extra=forbid，CLI/非空环境变量/文件优先级不变。没有新增apiKey/apiBase。

| 模型及路径 | 字段、默认与约束 |
|---|---|
| IntentOptions，understanding.intent | strategy=hybrid；rules/llm/hybrid |
| RewriteOptions，understanding.rewrite | strategy=hybrid；local/llm/hybrid；targets=[skills,history,memory,code]；maxQueriesPerTarget=2，范围1–2（含原文） |
| UnderstandingModelOptions，understanding.llm | model=null继承主模型；timeoutSeconds=10，范围(0,60]；maxExtraCallsPerTurn=1、maxAttempts=1固定；inputTokens=4096，范围512–8192；outputTokens=1024，范围256–2048 |
| AmbiguityOptions，understanding.ambiguity | policy=ask，支持model_decide；maxPreflightRounds=2，范围1–3；onUnresolved=interrupt固定 |
| UnderstandingOptions | enabled=true；四个子模型按上述默认构造 |
| PermissionOptions，permissions | mode=task_scoped，支持ask/read_only；defaultDecision=ask，支持deny；grantScopes=[once,turn]；两个子模型如下 |
| ShellPermissions，permissions.shell | mode=structured_only、unknownCommand=deny固定 |
| McpPermissions，permissions.mcp | unknownTool=ask，支持deny |

hybrid触发条件是当前实现的保守规则：检测到指代/继续类词、两个以上动作或unknown域。没有概率置信度。两个组件独立决定是否请求模型，但一轮只共享一次联合网络尝试；`rules+llm`也只有改写消费模型结果。模型失败/超时/截断/不合法输出回退，不做第二次修复推理。取消向上抛，不当成普通超时。

## 数据结构与构造点

| 类型 | 字段、必填性与默认 | 构造/消费 |
|---|---|---|
| Intent（Pydantic） | domain=unknown，coding/general/mixed/unknown；actions=[]，最多6个explain/locate/review/edit/plan_tests/run_tests | local_analysis或模型intent解析；用于提示和可观测性，不能授权 |
| QueryPlan（Pydantic） | standalone_query=''≤8000字符；queries={}，只允许四种target；source_ids=[]≤20；unresolved=[]≤5 | 本地/模型构造；模型后经validate_rewrite；本地检索和默认澄清消费 |
| JointAnalysis（Pydantic） | intent、rewrite必填；extra=forbid | 生成私有kaki_understanding_build函数Schema，消费后不进入业务tool_runs |
| RequestContext（dataclass） | original_text、original_message_id、intent、rewrite必填；clarifications=[]；strategy={}；fallback=null | prepare构造，Runtime补真实澄清，AgentLoop/ContextEngine/工具消费；human_text只组合实际人类输入 |
| PreparedOperation（frozen dataclass） | name、capability、resource、fingerprint必填；argv=()；file_state=null | PermissionService.prepare构造；resource规范路径/站点，fingerprint为规范参数JSON的SHA256；文件写入记录现有内容SHA256或missing，授权/快照后重算 |
| ToolResult新增字段 | executed=True、denied=False、decision_id=null | 默认兼容旧工具构造；预检未执行/权限拒绝设false；执行后可关联q_，Store和UI保存/消费 |
| Runtime.question（dict） | id/future/kind/question/options/operation/session_id/turn_id | DecisionService.request构造；Future只在内存；Engine.live去掉Future |
| Decision回答（dict） | text必填；approval有choice；clarification返回message_id；request返回decision_id | Runtime.answer委托Service；精确选项校验后Store事务保存，再解除Future |

RequestContext.prompt不覆盖Message.content；metadata保存策略、校验回退和source IDs。当前只校验证据来源存在与字面约束，不保证模型语义判断永远正确。英文/代码标识符用精确保留，数字和部分否定规则校验；复杂自然语言权限采用保守规则，需要时询问，不将其描述成形式化语义证明。

## 模型接口与预算

Provider.complete新增max_attempts=4，主loop保留有限重试；理解调用显式传1，SDK仍max_retries=0。每次实际尝试独立llm_记录，kind=understanding；Store.usage动态按kind累计，不混为主loop迭代次数。无全局理解缓存，每Turn的联合结果复用。

理解输入仅当前任务和本会话最近6条user/assistant证据（通常约3轮），历史总预算2048 tokens、单条最多700，再以完整wire输入及Schema做inputTokens门。超预算走本地，不裁掉当前原始任务以假装成功。主任务仍走ContextEngine原有窗口/Curator/最终预算门，默认65536窗口、8192输出。

## 检索接口与兼容

- `fused_rank(queries,documents,min_score=0)`：每条query先BM25，原文权重2、扩展权重1，累加weight/(60+rank)，按文档位置去重。localMinScore仍作用于BM25原始得分，不能拿旧阈值和RRF得分比较。
- `Skills.resolve(original,queries=None)`：显式`$name`/`/skill name`只读取原文；派生query不能伪造显式Skill调用。
- `Memory.recall(query,limit=None,queries=None)`、segment(query,queries=None)：仅项目/用户范围，原文和扩展共同召回。
- ContextEngine.assemble(...,request=None)：prefix消费skills/memory查询，历史相关度消费history查询，派生system仍纳入窗口预算。
- `kaki_search`新增可选use_task_queries=false；显式工具query始终先搜索，opt-in最多再搜索一个派生字面query；regex模式不扩展，保持同path/glob、顺序执行、输出去重。不改写命令、编辑内容和正则表达式。
- 主loop的普通ask_user回答会清除当前code查询扩展，避免后续沿用过时目标；不会重置理解模型预算。

## 权限、交互和恢复

能力：workspace.read/write、session.read、interaction.ask_user、test.execute、memory.write、browser.observe/interact。未知工具默认无权限。当前用户原文和真实澄清可决定任务范围；模型/文件/网页/记忆/Skill不可授予权限。read_only为配置上限；明确不写入/不运行测试/排除路径优先。复杂任务措辞可能需要询问，不能由LLM标签补授权。

任务范围未覆盖时，defaultDecision=ask请求用户选择once/turn/deny。once只适用于当前固定操作；turn范围仍限定当前Turn及资源（文件路径、cwd+argv、浏览器操作指纹），新Turn清空。授权UI给出工作区、会话、Turn、能力、资源、argv、指纹、写入前hash及参数预览。不能接受“可以，但别改tests”这种混合自由文本为授权，必须拒绝后提交新约束。

浏览器工具逐名分类：navigate仅HTTP(S)且无内嵌凭据；观察类可用，交互/未知类询问或deny；run_code/evaluate/install/file_upload直接拒绝。配置MCP启动是已信任基础设施，不等于批准页面提交或任意JS。当前不提供OS进程沙箱和浏览器网络防火墙，无法保证获准测试或网页的内在副作用隔离。

SQLite schema3新增decisions，外键关联session/turn；取消或进程恢复将pending设expired。已回答记录保留用于审计，临时能力不恢复，不自动重放UNKNOWN工具。授权文本不进入用户原始任务，澄清文本保留为真实同Turn消息。

兼容测试：test_understanding_permissions.py、test_understanding_ui.py、test_config_workspace.py；原Store/Runtime/Context/Provider/TUI/App回归继续执行。开发验收包括真实RightCode两轮TUI对话、App修复/测试/review/追问，以及SQLite证据、独立测试与界面截图。
