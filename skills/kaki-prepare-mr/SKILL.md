---
name: kaki-prepare-mr
description: 根据当前开发分支和真实验证结果准备MR/PR标题正文、检查精确提交范围，按明确授权推送并创建或复用GitHub/Codebase草稿MR；不合并、不部署。
metadata:
  kaki:
    required_tools: [kaki_git, kaki_mr]
---

先确认工作区、开发分支、目标分支、HEAD提交与验证结果，不把用户未提交或其它任务改动带入MR。正文说明具体问题、最终行为、测试命令/结果、文档核对和已知限制；未运行的验证不能写通过。

先用`kaki_mr(action="prepare",base="...",title="...",body="...")`生成可审阅产物，该操作不创建远端MR。远端分支尚未准备时，按明确授权调用kaki_git push；配置启用只代表能力存在，实际推送/创建仍会要求单次批准。

使用kaki_tool_search查找并加载kaki_mr。创建前查询匹配head/base的已有MR，优先复用。创建失败或结果未知时保留receipt，只查询原请求结果，不换回执路径重发。工具不提供合并、force push、发布或部署能力。
