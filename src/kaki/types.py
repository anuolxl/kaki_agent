"""Domain values shared across UI, execution, context and storage."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

TurnStatus = Literal["queued", "running", "waiting_user", "completed", "interrupted", "cancelled", "failed"]
TERMINAL = {"completed", "interrupted", "cancelled", "failed"}


def now() -> str:
    return datetime.now(UTC).isoformat()


def uid(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex


@dataclass
class Message:
    role: str
    content: str = ""
    id: int = 0
    session_id: str = ""
    turn_id: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    native_items: list[dict[str, Any]] = field(default_factory=list)
    parts: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=now)

    def payload(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": self.tool_calls,
            "tool_call_id": self.tool_call_id,
            "native_items": self.native_items,
            "parts": self.parts,
            "artifacts": self.artifacts,
        }


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    text: str
    failed: bool = False
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    parts: list[dict[str, Any]] = field(default_factory=list)
    exit_code: int | None = None
    touched_paths: list[str] = field(default_factory=list)


@dataclass
class ModelResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    native_items: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = "completed"
    model: str = ""
    call_id: str = ""
    provider_id: str = ""


@dataclass(frozen=True)
class Event:
    type: str
    turn_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnResult:
    turn_id: str
    status: TurnStatus
    text: str
    stop_reason: str
    tools: int = 0
    tool_failures: int = 0
    verification: dict[str, Any] = field(default_factory=lambda: {"status": "not_run", "commands": []})
    usage: dict[str, Any] = field(default_factory=dict)
    before_snapshot: str | None = None
    after_snapshot: str | None = None
    error: str | None = None


class KakiError(Exception):
    """An actionable public error, without credentials or raw transport dumps."""


class ContextOverflow(KakiError):
    pass


class ProviderError(KakiError):
    def __init__(self, message: str, category: str = "provider", *, partial: bool = False):
        super().__init__(message)
        self.category = category
        self.partial = partial


@dataclass(frozen=True)
class TokenBudget:
    context_length: int
    reserved_output: int
    reserved_system: int
    reserved_tools: int
    reserved_user: int
    margin: int

    @property
    def input_limit(self) -> int:
        return self.context_length - self.reserved_output - self.margin

    @property
    def available_history(self) -> int:
        return max(0, self.input_limit - self.reserved_system - self.reserved_tools - self.reserved_user)


@dataclass
class ContextPlan:
    include_message_ids: list[int] = field(default_factory=list)
    include_archive_refs: list[str] = field(default_factory=list)
    drop_message_ids: list[int] = field(default_factory=list)
    working_state_injection: str = ""
    memory_sections: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class AssembledContext:
    messages: list[Message]
    budget: TokenBudget
    estimated_tokens: int
    metadata: dict[str, Any] = field(default_factory=dict)
