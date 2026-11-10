"""Right Code Responses transport. The loop never parses SSE or owns credentials."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from openai import APIConnectionError, APITimeoutError, AsyncOpenAI

from kaki.config import Config, safe_error
from kaki.types import Message, ModelResponse, ProviderError, ToolCall, now, uid

Record = Callable[[dict], Awaitable[None]]
Delta = Callable[[str, str], Awaitable[None]]


def wire_input(messages: list[Message], resolve_part=None) -> tuple[str, list[dict[str, Any]]]:
    instructions = []
    items = []
    for m in messages:
        if m.role == "system":
            instructions.append(m.content)
        elif m.role == "user":
            parts = [{"type": "input_text", "text": m.content or "(empty)"}]
            parts.extend(expand_parts(m.parts, resolve_part))
            items.append({"role": "user", "content": parts})
        elif m.role == "assistant":
            if m.native_items:
                # Preserve the exact item ordering and opaque reasoning needed by the tool round-trip.
                items.extend(m.native_items)
            else:
                if m.content:
                    items.append(
                        {"role": "assistant", "content": [{"type": "output_text", "text": m.content}]}
                    )
                for call in m.tool_calls:
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call["id"],
                            "name": call["name"],
                            "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                        }
                    )
        elif m.role == "tool":
            output: Any = m.content or "(no output)"
            if m.parts:
                output = [{"type": "input_text", "text": output}, *expand_parts(m.parts, resolve_part)]
            items.append({"type": "function_call_output", "call_id": m.tool_call_id, "output": output})
        else:
            raise ProviderError(f"不支持的消息角色：{m.role}", "protocol")
    return "\n\n".join(instructions), items


def expand_parts(parts, resolver):
    result = []
    for p in parts:
        if "artifact_id" in p:
            result.append(
                resolver(p)
                if resolver
                else {"type": "input_image", "image_url": "artifact://" + p["artifact_id"]}
            )
        else:
            result.append(p)
    return result


def normalize_response(response: Any, call_id: str) -> ModelResponse:
    data = response.model_dump(exclude_none=True) if hasattr(response, "model_dump") else response
    native = data.get("output", [])
    text = []
    calls = []
    for item in native:
        typ = item.get("type")
        if typ == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    text.append(part.get("text", ""))
                elif part.get("type") == "refusal":
                    text.append(part.get("refusal", ""))
        elif typ == "function_call":
            if not item.get("call_id") or not item.get("name"):
                raise ProviderError("模型返回缺少call_id/name的工具请求，未执行", "protocol")
            try:
                args = json.loads(item.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {"_raw_arguments": item.get("arguments")}
            except ValueError:
                args = {"_raw_arguments": item.get("arguments")}
            calls.append(ToolCall(item["call_id"], item["name"], args))
    raw_usage = data.get("usage") or {}
    usage = {}
    if raw_usage:
        a = int(raw_usage.get("input_tokens", 0) or 0)
        b = int(raw_usage.get("output_tokens", 0) or 0)
        usage = {
            "prompt_tokens": a,
            "completion_tokens": b,
            "total_tokens": int(raw_usage.get("total_tokens", a + b) or a + b),
            "cached_tokens": int((raw_usage.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0),
            "reasoning_tokens": int(
                (raw_usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0) or 0
            ),
        }
    status = data.get("status") or "completed"
    if status == "incomplete":
        finish = (data.get("incomplete_details") or {}).get("reason", "incomplete")
    elif status == "completed":
        finish = "completed"
    else:
        raise ProviderError("模型响应未成功结束：" + str(status), "response_failed")
    return ModelResponse(
        "".join(text), calls, native, usage, finish, data.get("model", ""), call_id, data.get("id", "")
    )


def classification(exc: BaseException) -> str:
    code = getattr(exc, "status_code", None)
    text = str(exc).lower()
    if "context" in text and any(x in text for x in ["length", "window", "maximum", "too long", "limit"]):
        return "context_overflow"
    if code in (401, 403):
        return "authentication"
    if code == 429:
        return "rate_limit"
    if isinstance(exc, (APIConnectionError, APITimeoutError, TimeoutError)):
        return "network"
    if code and code >= 500:
        return "upstream"
    return "provider"


class RightCodeProvider:
    def __init__(self, config: Config, record: Record | None = None, part_resolver=None):
        config.validate_credentials()
        self.config = config
        self.record = record
        self.part_resolver = part_resolver
        p = config.providers.openai
        self.client = AsyncOpenAI(
            api_key=p.api_key.get_secret_value(),
            base_url=p.api_base.rstrip("/"),
            default_headers=p.extra_headers or {},
            timeout=p.timeout,
            max_retries=0,
        )
        self.disabled_parameters: set[str] = set()

    async def close(self):
        await self.client.close()

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        turn_id: str = "",
        kind: str = "main",
        on_delta: Delta | None = None,
        tool_choice: Any = "auto",
    ) -> ModelResponse:
        model = model or self.config.agents.defaults.model
        api_model = model.removeprefix("openai/")
        instructions, items = await asyncio.to_thread(wire_input, messages, self.part_resolver)
        kwargs = {
            "model": api_model,
            "input": items,
            "instructions": instructions,
            "stream": True,
            "max_output_tokens": max_tokens or self.config.agents.defaults.max_tokens,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "temperature": self.config.agents.defaults.temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        # No implicit fallback to another model or endpoint. Only unsupported optional parameters may be omitted.
        for attempt in range(4):
            call_id = uid("llm_")
            started = now()
            visible = False
            stream = None
            record = {
                "id": call_id,
                "turn_id": turn_id,
                "kind": kind,
                "model": model,
                "status": "running",
                "created": started,
                "metadata": {
                    "attempt": attempt + 1,
                    "protocol": "responses",
                    "omitted_parameters": sorted(self.disabled_parameters),
                },
            }
            if self.record:
                await self.record(record)
            try:
                active = {k: v for k, v in kwargs.items() if k not in self.disabled_parameters}
                response = None
                t0 = time.monotonic()
                stream = await self.client.responses.create(**active)
                async for event in stream:
                    typ = event.type
                    if typ == "response.output_text.delta":
                        visible = True
                        if on_delta:
                            await on_delta(event.delta, call_id)
                    elif typ in ("response.completed", "response.incomplete"):
                        response = normalize_response(event.response, call_id)
                    elif typ in ("error", "response.failed"):
                        err = getattr(event, "error", None) or getattr(
                            getattr(event, "response", None), "error", None
                        )
                        raise ProviderError(
                            safe_error(getattr(err, "message", None) or err or typ),
                            "response_failed",
                            partial=visible,
                        )
                if response is None:
                    raise ProviderError(
                        "模型流提前断开，没有完整结束事件；未执行不完整工具请求",
                        "stream_incomplete",
                        partial=visible,
                    )
                if response.finish_reason != "completed" and response.tool_calls:
                    raise ProviderError(
                        "模型工具参数或响应被截断，未执行工具", "output_limit", partial=visible
                    )
                record.update(
                    status="completed",
                    ended=now(),
                    usage=response.usage,
                    metadata={
                        **record["metadata"],
                        "provider_id": response.provider_id,
                        "duration_ms": round((time.monotonic() - t0) * 1000),
                        "finish_reason": response.finish_reason,
                    },
                )
                if self.record:
                    await self.record(record)
                return response
            except asyncio.CancelledError:
                record.update(status="cancelled", ended=now())
                if self.record:
                    await asyncio.shield(self.record(record))
                raise
            except Exception as exc:
                category = exc.category if isinstance(exc, ProviderError) else classification(exc)
                record.update(
                    status="failed", ended=now(), metadata={**record["metadata"], "error_category": category}
                )
                if self.record:
                    await self.record(record)
                optional = next(
                    (
                        key
                        for key in ["temperature", "include", "store"]
                        if key not in self.disabled_parameters
                        and key in str(exc).lower()
                        and any(
                            s in str(exc).lower()
                            for s in ["unsupported", "not supported", "unknown parameter", "not allowed"]
                        )
                    ),
                    None,
                )
                if optional and not visible and attempt < 3:
                    self.disabled_parameters.add(optional)
                    continue
                if not visible and category in ("network", "rate_limit", "upstream") and attempt < 2:
                    await asyncio.sleep(min(2**attempt, 4))
                    continue
                public = safe_error(exc)
                if category == "authentication":
                    public = "Right Code认证失败；检查providers.openai.apiKey及其绑定渠道。"
                raise ProviderError(public, category, partial=visible) from None
            finally:
                if stream is not None:
                    await stream.close()
        raise ProviderError("模型有限重试已用尽")
