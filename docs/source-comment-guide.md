# 中文源码注释阅读入口

本轮注释使用普通中文注释，不改 docstring、配置常量、工具描述或模型提示词。这样 Typer 的 help 和 Pydantic 生成的 Schema 也保持不变。

按实际调用顺序阅读：

1. cli.entry：全局配置/工作目录如何分流到 desktop.launch 或 terminal_ui。
2. desktop：Instance 单例、Cocoa 主线程、NativeService 后台事件循环与退出握手；TUI 从 KakiApp.on_mount/initialize_runtime 进入。
3. workspace/config：规范单 Git 根、状态隔离、配置合并与密钥边界。
4. Runtime.initialize：Store、Provider、ContextEngine、Tools 和可选 MCP 的资源归属。
5. AppController.send/_execute 或 KakiApp.submit_text/watch_turn：用户输入怎样进入同一个 Runtime.submit。
6. Runtime._turn：幂等接收、一次理解/澄清、首次变更快照与完整终态。
7. AgentLoop.run：窗口复核、模型终事件、意图先落库、工具观察和有界退出。
8. provider：Responses 原生项与 SSE；tools/permissions/decisions/development：Schema、审批、执行、Git/profile/MR。
9. context 与 understanding：原文、检索提示、token 预留、归档及语义边界。
10. Store 与 Checkpoint/Recovery：事实与投影的区别、UNKNOWN 不重放、用户 index/HEAD 不受影子快照影响。
11. App/TUI/frontend：phase、draft、busy、断线 unknown；测试通过与 Turn completed 分开判断。

数据结构先看 types.py、understanding/types.py，再看 Engine、Tool、PreparedOperation、Workspace 与配置类旁的字段注释。重要变量旁解释 ID、作用域、单位、线程/协程归属与恢复约束，不逐行复述赋值语句。

验证采用完整 Python AST 相等比较，保留所有原 docstring/字符串值；前端仅增加注释并核对语法。完整回归与 CI 仍是合入条件。

async/await 在此处主要组织同一事件循环中的协作；NativeService、Store 执行器及显式 to_thread 才是需要区分的线程边界。self 指当前实例，不能把当前项目的 self.runtime 理解为全局只有一个 Runtime。
