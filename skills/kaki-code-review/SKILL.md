---
name: kaki-code-review
description: 对当前仓库的diff、分支或指定文件做只读代码评审，核对缺陷、回归风险、测试证据和SPEC一致性；不自行修改、暂存、提交或部署。
metadata:
  kaki:
    required_tools: [kaki_git, kaki_read_file, kaki_search]
---

确定评审基线与文件范围，读取规则和相关spec，再看完整diff及被影响的调用方。优先报告可复现的问题，给出文件、触发条件、后果与证据，区分确认缺陷和待验证假设。测试未执行或平台限制必须明确；命令退出0和某条日志不能替代业务验收。

使用kaki_git查询状态/历史、kaki_exec执行已允许的git diff，以及只读文件工具。模型给出的edit意图不授权修改；用户要求修复时重新明确范围，使用开发Skill。
