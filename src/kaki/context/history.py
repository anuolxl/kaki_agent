"""Tool-group closure and whole-user-turn selection, informed by Pico HistoryTrimmer.

Upstream reference: htxoffical/pico-harness d6c7a648, Apache-2.0; see NOTICE.
"""

from __future__ import annotations

from dataclasses import replace

from kaki.types import ContextOverflow, Message

from .tokens import estimate


def groups(messages: list[Message]) -> list[list[Message]]:
    result = []
    for m in messages:
        if m.role == "user" or not result:
            result.append([])
        result[-1].append(m)
    return result


def structural_errors(messages: list[Message]) -> list[str]:
    pending = {}
    errors = []
    for m in messages:
        if m.role == "assistant":
            if pending:
                errors.append("assistant arrived before previous tool results: " + ",".join(pending))
            for c in m.tool_calls:
                if not c.get("id") or c["id"] in pending:
                    errors.append("missing or duplicate tool_call_id")
                else:
                    pending[c["id"]] = m.id
        elif m.role == "tool":
            if m.tool_call_id not in pending:
                errors.append("orphan tool result: " + str(m.tool_call_id))
            else:
                del pending[m.tool_call_id]
        elif m.role == "user" and pending:
            errors.append("user message interrupts open tool exchange")
    if pending:
        errors.append("missing tool results: " + ",".join(pending))
    return errors


def canonical(messages: list[Message], ids: list[int]) -> list[Message]:
    selected = set(ids)
    by_call = {}
    children = {}
    for m in messages:
        if m.role == "assistant":
            for c in m.tool_calls:
                by_call[c["id"]] = m.id
        elif m.role == "tool":
            children.setdefault(m.tool_call_id, []).append(m.id)
    changed = True
    while changed:
        changed = False
        for call, parent in by_call.items():
            related = {parent, *children.get(call, [])}
            if related & selected and not related <= selected:
                selected |= related
                changed = True
    # A complete user boundary is mandatory for history; do not manufacture an assistant-led past.
    result = [m for m in messages if m.id in selected]
    first = next((i for i, m in enumerate(result) if m.role == "user"), len(result))
    return result[first:]


def trim(
    history: list[Message],
    ids: list[int],
    protected: set[int],
    relevance: dict[int, float],
    system: Message,
    user: Message,
    tools: list[dict],
    input_limit: int,
) -> tuple[list[Message], dict]:
    chosen = canonical(history, ids)
    dropped = []
    while estimate([system, *chosen, user], tools) > input_limit:
        removable = [g for g in groups(chosen) if not any(m.id in protected for m in g)]
        if not removable:
            break
        drop = min(removable, key=lambda g: (sum(relevance.get(m.id, 0.5) for m in g) / len(g), g[0].id))
        mids = {m.id for m in drop}
        dropped.extend(mids)
        chosen = [m for m in chosen if m.id not in mids]
    window = [system, *chosen, user]
    errors = structural_errors(window)
    tokens = estimate(window, tools)
    return chosen, {
        "ok": not errors and tokens <= input_limit,
        "estimated_tokens": tokens,
        "input_limit": input_limit,
        "over_by": max(0, tokens - input_limit),
        "dropped_ids": dropped,
        "errors": errors,
        "included_ids": [m.id for m in chosen],
    }


def shrink_tool_text(messages: list[Message], keep: int = 3) -> tuple[list[Message], int]:
    indices = [i for i, m in enumerate(messages) if m.role == "tool"]
    old = set(indices[:-keep] if keep else indices)
    placeholder = (
        "[Kaki: earlier tool output omitted from active window; full observation remains in session/archive.]"
    )
    result = []
    changed = 0
    for i, m in enumerate(messages):
        if i in old and m.content != placeholder:
            result.append(replace(m, content=placeholder, parts=[]))
            changed += 1
        else:
            result.append(m)
    return result, changed


def assert_window(messages: list[Message], tools: list[dict], limit: int):
    errors = structural_errors(messages)
    if errors:
        raise ContextOverflow("模型窗口工具配对不合法：" + "; ".join(errors))
    tokens = estimate(messages, tools)
    if tokens > limit:
        raise ContextOverflow(f"上下文仍超限：估算{tokens}，允许输入{limit}；请缩小当前输入或工具输出")
    return tokens
