"""A bounded private context planner; it cannot access code/Shell/browser tools."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

from kaki.types import AssembledContext, ContextPlan, KakiError, Message, now

from .knowledge import rank
from .tokens import count_text, estimate, excerpt

SCHEMAS = {
    "budget": {"plan": {"type": "object"}},
    "archive": {
        "message_ids": {"type": "array", "items": {"type": "integer"}},
        "reason": {"type": "string"},
        "summary": {"type": "string"},
    },
    "retrieve": {
        "archive_ref": {"type": "string"},
        "offset": {"type": "integer"},
        "limit": {"type": "integer"},
    },
    "search": {"query": {"type": "string"}},
    "memory": {"query": {"type": "string"}},
    "relevance": {"updates": {"type": "array", "items": {"type": "object"}}},
    "state": {
        "goals": {"type": "array", "items": {"type": "string"}},
        "open_threads": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
    },
    "build": {
        "include_message_ids": {"type": "array", "items": {"type": "integer"}},
        "include_archive_refs": {"type": "array", "items": {"type": "string"}},
        "drop_message_ids": {"type": "array", "items": {"type": "integer"}},
        "memory_sections": {"type": "array", "items": {"type": "string"}},
        "working_state_injection": {"type": "string"},
        "notes": {"type": "string"},
    },
}
DESCRIPTIONS = {
    "budget": "Validate a candidate context plan against complete prompt and tool-pair budgets.",
    "archive": "Losslessly archive old message IDs. Source transcript remains unchanged.",
    "retrieve": "Read an exact paginated archive. Never invent archived content.",
    "search": "Find historical message IDs relevant to the current task.",
    "memory": "Read relevant project memory, with sources.",
    "relevance": "Set relevance for existing message IDs, score within 0..1.",
    "state": "Save bounded goals, unresolved tasks and only the active version of decisions.",
    "build": "Submit the final context plan; after accepted=true, stop immediately.",
}


def parse_plan(data: dict) -> ContextPlan:
    fields = ContextPlan.__dataclass_fields__
    unknown = set(data) - set(fields)
    if unknown:
        raise KakiError("未知ContextPlan字段：" + ",".join(unknown))
    for k in ["include_message_ids", "drop_message_ids"]:
        if k in data and (not isinstance(data[k], list) or not all(type(x) is int for x in data[k])):
            raise KakiError(k + "必须是整数数组")
    for k in ["include_archive_refs", "memory_sections"]:
        if k in data and (not isinstance(data[k], list) or not all(isinstance(x, str) for x in data[k])):
            raise KakiError(k + "必须是字符串数组")
    for key in ["working_state_injection", "notes"]:
        if key in data and not isinstance(data[key], str):
            raise KakiError(key + "必须是字符串")
    return ContextPlan(**data)


class Curator:
    def __init__(self, engine):
        self.engine = engine

    async def trace(self, session: str, turn: str, event: str, payload: dict):
        root = self.engine.workspace.state / "context" / "traces" / session
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (root / (turn + ".jsonl")).open("a") as f:
            f.write(json.dumps({"at": now(), "event": event, "payload": payload}, ensure_ascii=False) + "\n")

    async def run(
        self,
        session: str,
        history: list[Message],
        user: Message,
        system: Message,
        tools: list[dict],
        budget,
        manifest: dict,
        working: dict,
        turn_id: str,
    ) -> tuple[AssembledContext | None, dict]:
        e = self.engine
        private_tools = [
            {
                "type": "function",
                "name": "kaki_context_" + name,
                "description": DESCRIPTIONS[name],
                "parameters": {"type": "object", "properties": props, "additionalProperties": False},
                "strict": False,
            }
            for name, props in SCHEMAS.items()
            if name != "memory" or e.config.memory.backend is not None
        ]
        # The planner sees a bounded manifest, not an unbounded duplicate of the transcript.
        recent = history[-18:]
        ranked = rank(user.content, [m.content for m in history])[:12]
        selected = {m.id: m for m in recent}
        for i, _ in ranked:
            selected[history[i].id] = history[i]
        for g in e.protected_groups(history):
            for m in g:
                selected[m.id] = m
        payload = {
            "current_request": excerpt(user.content, 600),
            "budget": asdict(budget),
            "working_state": working,
            "manifest": [
                {
                    "id": m.id,
                    "role": m.role,
                    "turn_id": m.turn_id,
                    "tokens": count_text(m.content),
                    "snippet": excerpt(m.content, 120),
                    "relevance": manifest.get(str(m.id), {}).get("relevance", 0.5),
                }
                for m in sorted(selected.values(), key=lambda m: m.id)
            ],
            "all_message_ids": [m.id for m in history],
        }
        messages = [
            Message(
                "system",
                "You are Kaki Curator, a private context manager. Never answer the user, invent historical content or use external tools. Select existing IDs, preserve initial constraints and current user request. Keep complete tool exchanges. Prefer recent/relevant evidence and unresolved tasks. Only active user decisions belong in working state. Archive before omitting old evidence when useful. Finish with kaki_context_build; Python will add the current user. Include a concise working_state_injection. Minimize latency: when history fits the budget, call build in your FIRST response using all_message_ids, without archive/state calls. For long histories, batch archive IDs in one call if needed and submit build by your THIRD response; Python validates, closes tool groups, trims to budget and losslessly archives all omitted source facts. Avoid spending the entire deadline on preparatory calls.",
            ),
            Message("user", json.dumps(payload, ensure_ascii=False)),
        ]
        try:
            async with asyncio.timeout(e.config.context.curator_timeout_seconds):
                for step in range(1, 13):
                    if estimate(messages, private_tools) > budget.context_length - 2048 - budget.margin:
                        return None, {"fallback_reason": "curator_input_limit"}
                    response = await e.provider.complete(
                        messages,
                        private_tools,
                        model=e.config.context.curator_model,
                        max_tokens=min(2048, budget.reserved_output),
                        turn_id=turn_id,
                        kind="curator",
                    )
                    await self.trace(
                        session,
                        turn_id,
                        "curator.response",
                        {
                            "step": step,
                            "call_id": response.call_id,
                            "tools": [c.name for c in response.tool_calls],
                            "text": response.text,
                        },
                    )
                    if not response.tool_calls:
                        return None, {"fallback_reason": "invalid_plan"}
                    messages.append(
                        Message(
                            "assistant",
                            response.text,
                            tool_calls=[asdict(c) for c in response.tool_calls],
                            native_items=response.native_items,
                        )
                    )
                    for call in response.tool_calls:
                        try:
                            name = call.name.removeprefix("kaki_context_")
                            a = call.arguments
                            output = {}
                            accepted = None
                            if call.name not in {x["name"] for x in private_tools}:
                                raise KakiError("未注册的Curator工具")
                            if name in ("build", "budget"):
                                plan = parse_plan(a.get("plan", {}) if name == "budget" else a)
                                accepted = await e.build_plan(
                                    session, history, user, system, tools, budget, manifest, plan
                                )
                                output = {
                                    "accepted": True,
                                    "estimated_tokens": accepted.estimated_tokens,
                                    "included_ids": accepted.metadata["included_ids"],
                                }
                            elif name == "archive":
                                ref = await e.archive.save(
                                    session,
                                    history,
                                    a.get("message_ids", []),
                                    a.get("reason", ""),
                                    a.get("summary", ""),
                                )
                                for mid in a.get("message_ids", []):
                                    manifest.setdefault(str(mid), {})["archive_ref"] = ref
                                await e.store.run("context_set", session, manifest=manifest)
                                output = {"archive_ref": ref}
                            elif name == "retrieve":
                                output = await e.archive.read(
                                    session,
                                    a["archive_ref"],
                                    a.get("offset", 0),
                                    min(a.get("limit", 4000), 8000),
                                )
                            elif name == "search":
                                output = {
                                    "hits": [
                                        {
                                            "id": history[i].id,
                                            "score": s,
                                            "snippet": excerpt(history[i].content, 200),
                                        }
                                        for i, s in rank(a.get("query", ""), [m.content for m in history])[
                                            :12
                                        ]
                                    ]
                                }
                            elif name == "memory":
                                output = {
                                    "memory": [
                                        {"id": x["id"], "content": x["content"], "sources": x["source_ids"]}
                                        for x in await e.memory.recall(a.get("query", user.content))
                                    ]
                                }
                            elif name == "relevance":
                                allowed = {m.id for m in history}
                                for update in a.get("updates", []):
                                    mid = update.get("message_id")
                                    value = update.get("relevance")
                                    if (
                                        mid not in allowed
                                        or not isinstance(value, (float, int))
                                        or not 0 <= value <= 1
                                    ):
                                        raise KakiError("相关性ID/数值无效")
                                    manifest.setdefault(str(mid), {})["relevance"] = value
                                await e.store.run("context_set", session, manifest=manifest)
                                output = {"updated": True}
                            elif name == "state":
                                working = {
                                    k: [str(x)[:800] for x in a.get(k, [])][:limit]
                                    for k, limit in [("goals", 8), ("open_threads", 12), ("decisions", 20)]
                                }
                                await e.store.run("context_set", session, working=working)
                                output = working
                            else:
                                raise KakiError("未知Curator工具")
                            if name == "build" and accepted:
                                accepted.metadata.update(path="slow", curator_steps=step)
                                await self.trace(session, turn_id, "curator.accepted", accepted.metadata)
                                return accepted, {}
                        except (KakiError, ValueError, KeyError, TypeError) as exc:
                            output = {"accepted": False, "error": str(exc), "retry_allowed": True}
                        messages.append(
                            Message(
                                "tool",
                                excerpt(json.dumps(output, ensure_ascii=False), 2000),
                                tool_call_id=call.id,
                            )
                        )
                        await self.trace(
                            session, turn_id, "curator.tool", {"name": call.name, "output": output}
                        )
                return None, {"fallback_reason": "curator_step_limit"}
        except TimeoutError:
            return None, {"fallback_reason": "curator_timeout"}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return None, {
                "fallback_reason": "curator_error",
                "error_category": getattr(exc, "category", type(exc).__name__),
            }
