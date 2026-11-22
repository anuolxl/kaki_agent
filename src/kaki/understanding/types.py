from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: Literal["coding", "general", "mixed", "unknown"] = "unknown"
    actions: list[Literal["explain", "locate", "review", "edit", "plan_tests", "run_tests"]] = Field(
        default_factory=list, max_length=6
    )


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    standalone_query: str = Field(default="", max_length=8000)
    queries: dict[Literal["skills", "history", "memory", "code"], list[str]] = Field(default_factory=dict)
    source_ids: list[int] = Field(default_factory=list, max_length=20)
    unresolved: list[str] = Field(default_factory=list, max_length=5)


class JointAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Intent
    rewrite: QueryPlan


@dataclass
class RequestContext:
    original_text: str
    original_message_id: int
    intent: Intent
    rewrite: QueryPlan
    clarifications: list[dict] = field(default_factory=list)
    strategy: dict = field(default_factory=dict)
    fallback: str | None = None

    @property
    def human_text(self):
        return "\n".join([self.original_text, *(c["text"] for c in self.clarifications)])

    def queries_for(self, target):
        return self.rewrite.queries.get(target, [self.original_text])

    def prompt(self):
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
        return {
            "intent": self.intent.model_dump(),
            "rewrite": self.rewrite.model_dump(),
            "strategy": self.strategy,
            "fallback": self.fallback,
            "clarification_message_ids": [c["message_id"] for c in self.clarifications],
        }
