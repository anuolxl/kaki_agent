"""Conservative prompt accounting, including tool schemas and native protocol items."""

from __future__ import annotations

import json
from functools import lru_cache

import tiktoken

from kaki.provider import wire_input
from kaki.types import Message


@lru_cache(maxsize=1)
def encoding():
    return tiktoken.get_encoding("o200k_base")


def count_text(text: str) -> int:
    if not text:
        return 0
    try:
        return len(encoding().encode(text, disallowed_special=()))
    except Exception:
        return max(1, (len(text.encode("utf-8")) + 2) // 3)


def estimate(messages: list[Message], tools: list[dict] | None = None) -> int:
    system, items = wire_input(messages)
    # Ciphertext is transport, not tokenized natural-language reasoning. Use the
    # provider's entire output-token count as a conservative reasoning reserve.
    # Legacy/unknown-usage items keep the previous raw-wire fallback.
    known = set()
    reserve = 0
    for message in messages:
        opaque = [
            x for x in message.native_items if x.get("type") == "reasoning" and x.get("encrypted_content")
        ]
        if opaque and type(message.native_token_reserve) is int and message.native_token_reserve >= 0:
            known.update(id(x) for x in opaque)
            reserve += message.native_token_reserve
    counted = [
        {k: v for k, v in item.items() if k != "encrypted_content"} if id(item) in known else item
        for item in items
    ]
    # Image bytes remain in artifact storage. Reserve conservative image overhead, not base64 length.
    images = sum(len(m.parts) for m in messages)
    return (
        32
        + count_text(
            json.dumps({"instructions": system, "input": counted, "tools": tools or []}, ensure_ascii=False)
        )
        + reserve
        + images * 4096
    )


def excerpt(text: str, token_limit: int) -> str:
    try:
        e = encoding()
        ids = e.encode(text, disallowed_special=())
        return e.decode(ids[: max(1, token_limit)]) if len(ids) > token_limit else text
    except Exception:
        return text[: max(1, token_limit * 2)]
