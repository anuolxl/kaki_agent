# Kaki 当前架构

当前实现详解、控制流、数据流与使用指南统一见 [kaki-agent-guide.md](kaki-agent-guide.md)，字段和持久化约定见 [module-spec/core.md](module-spec/core.md)。

Kaki 的最小可用闭环为 CLI/Textual → Runtime → ContextEngine → AgentLoop → RightCodeProvider → ToolRegistry → 持久 ToolResult → 下一轮模型 → TurnResult。它是一个 Python 进程和一个 asyncio 循环；浏览器 MCP 是独立 stdio 子进程。SQLite 由独立线程拥有；耗时同步文件 I/O 通过线程执行。

Runtime 拥有工作区执行租约和 Turn 终态，AgentLoop 拥有有界推理循环，ContextEngine 拥有历史选择与预算门，Store 拥有持久事实。工具执行之前先提交完整模型消息与 planned 意图，然后提交 running；真实结果与工具消息一起提交。崩溃后 running 只能判为 unknown，不能自动重放。

UI 使用本地 Event 队列，不经过 RPC Server。会话查询和人工文件恢复目前从 TUI 直接调用 Store/Checkpoint，是明确存在的薄边界，不声称所有功能已收敛到 Runtime 外观。

Pico 的上下文思想以独立实现迁移：快慢路径、8 个 Curator 私有工具、manifest、working state、相关度衰减、无损归档、闭合工具调用组、整用户组裁剪、确定性回退、旧工具结果省略、循环内预算重平衡、溢出有限恢复与原生 reasoning 保留。Token 数为保守估算，不等同服务商的 tokenizer 承诺。

当前没有多 Agent、远程 worker、通用网关、Myna 服务或多仓事务。此取舍服务于个人本机、单 Git 仓库的首期目标。
