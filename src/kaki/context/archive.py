"""Lossless archives are derived from immutable transcript facts, never the sole copy."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from kaki.config import Config
from kaki.store import Store
from kaki.types import KakiError, Message, uid
from kaki.workspace import Workspace


class Archive:
    def __init__(self, workspace: Workspace, store: Store, config: Config):
        self.workspace, self.store = workspace, store
        self.root = (workspace.state / config.context.archive_dir).resolve()
        if self.root == workspace.state or not self.root.is_relative_to(workspace.state):
            raise KakiError("context.archiveDir必须位于项目状态目录内部")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    async def save(
        self,
        session: str,
        messages: list[Message],
        ids: list[int],
        reason: str = "",
        summary: str = "",
        tags: list[str] | None = None,
    ) -> str:
        requested = set(ids)
        selected = [m for m in messages if m.id in requested]
        if requested - {m.id for m in selected}:
            raise KakiError("归档请求包含不存在的消息ID")
        if not selected:
            raise KakiError("没有可归档的消息ID")
        payload = {
            "schema_version": 1,
            "session_id": session,
            "reason": reason,
            "messages": [asdict(m) for m in selected],
        }
        raw = json.dumps(payload, ensure_ascii=False).encode()
        digest = hashlib.sha256(raw).hexdigest()
        file = self.root / (digest + ".json")
        if not file.exists():
            tmp = self.root / (uid("tmp_") + ".json")
            tmp.write_bytes(raw)
            tmp.chmod(0o600)
            tmp.replace(file)
        artifact = {"path": str(file), "sha256": digest, "size": len(raw)}
        return await self.store.run(
            "archive_add", session, [m.id for m in selected], artifact, summary, tags or []
        )

    async def load(self, session: str, ref: str) -> tuple[dict, dict]:
        row = await self.store.run("archive_get", session, ref)
        path = Path(row["artifact"]["path"]).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise KakiError("归档文件缺失或引用非法；原会话仍保留")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row["artifact"]["sha256"]:
            raise KakiError("归档内容hash不匹配")
        return json.loads(raw), row

    async def read(self, session: str, ref: str, offset: int = 0, limit: int = 8000) -> dict:
        payload, row = await self.load(session, ref)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        offset = max(0, offset)
        limit = max(100, min(limit, 16000))
        end = offset + limit
        return {
            "archive_ref": ref,
            "text": text[offset:end],
            "offset": offset,
            "next_offset": end if end < len(text) else None,
            "sha256": row["artifact"]["sha256"],
            "message_ids": row["message_ids"],
        }
