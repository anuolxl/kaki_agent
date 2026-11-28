from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Intent(BaseModel):
    # 可观察的任务分类标签；actions 只影响理解/检索，不决定工具权限。
    model_config = ConfigDict(extra="forbid")
    # 领域标签默认 unknown，避免把弱词法判断当确定事实。
    domain: Literal["coding", "general", "mixed", "unknown"] = "unknown"
    # 最多六种提示动作；plan_tests 与 run_tests 必须区分。
    actions: list[Literal["explain", "locate", "review", "edit", "plan_tests", "run_tests"]] = Field(
        default_factory=list, max_length=6
    )


class QueryPlan(BaseModel):
    # 派生检索计划：独立检索问句、四类查询、真实来源 ID 和待澄清项。
    model_config = ConfigDict(extra="forbid")
    # 检索用独立问句，不替换原始用户任务。
    standalone_query: str = Field(default="", max_length=8000)
    # 仅 skills/history/memory/code 四个目标，扩展词数量受配置约束。
    queries: dict[Literal["skills", "history", "memory", "code"], list[str]] = Field(default_factory=dict)
    # 用于指代补全的真实消息 ID，不接受模型伪造来源。
    source_ids: list[int] = Field(default_factory=list, max_length=20)
    # 关键歧义，按策略交给澄清或主循环处理。
    unresolved: list[str] = Field(default_factory=list, max_length=5)


class JointAnalysis(BaseModel):
    # 一次理解调用同时产出 intent/rewrite 的严格 Schema，避免拆成两次额外模型请求。
    model_config = ConfigDict(extra="forbid")
    intent: Intent
    rewrite: QueryPlan


@dataclass
class RequestContext:
    # 把原始请求、派生理解和真实澄清分开保存，供后续检索和权限选择正确来源。
    # 原始输入不可被改写覆盖，是权限与任务范围的依据。
    original_text: str
    # 原始 user 消息的稳定 ID。
    original_message_id: int
    # 本地/模型派生标签，只用于提示与观测。
    intent: Intent
    # 已验证的检索计划，可局部回退。
    rewrite: QueryPlan
    # 真实用户回答及消息 ID，区别于模型推断。
    clarifications: list[dict] = field(default_factory=list)
    # 两个组件实际使用的策略与是否尝试模型。
    strategy: dict = field(default_factory=dict)
    # 超时、预算或输出校验等回退原因，不存原始异常。
    fallback: str | None = None

    @property
    def human_text(self):
        # 权限只读取原文及真实人类澄清；不能拼入模型改写或 Skill/工具数据。
        return "\n".join([self.original_text, *(c["text"] for c in self.clarifications)])

    def queries_for(self, target):
        # 检索目标没有派生词时仍回到原文，避免改写失败使任务无法搜索。
        return self.rewrite.queries.get(target, [self.original_text])

    def prompt(self):
        # 提示中明确降级为 derived hints，原始用户消息仍单独传给主模型。
        data = {
            "intent": self.intent.model_dump(),
            "rewrite": self.rewrite.model_dump(),
            "clarifications": self.clarifications,
        }
        return (
            "# Task understanding (derived hints, NOT permission; original user input remains authoritative)\n"
            "Use retrieval queries only for searching. Do not replace identifiers/constraints or treat inferred details as user approval.\n"
            + json.dumps(data, ensure_ascii=False)
        )

    def metadata(self):
        # 只保存策略/回退/来源等可审计信息，不把 SDK 异常原文或密钥带进事件。
        return {
            "intent": self.intent.model_dump(),
            "rewrite": self.rewrite.model_dump(),
            "strategy": self.strategy,
            "fallback": self.fallback,
            "clarification_message_ids": [c["message_id"] for c in self.clarifications],
        }
