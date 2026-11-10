"""SQLite facts, short transactions, and a single connection-owning worker."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from kaki.types import TERMINAL, KakiError, Message, ModelResponse, ToolResult, TurnResult, now, uid

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, title TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turns(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), request_id TEXT UNIQUE NOT NULL,
 status TEXT NOT NULL, text TEXT NOT NULL, stop_reason TEXT, error TEXT, result_json TEXT,
 before_snapshot TEXT, after_snapshot TEXT, created TEXT NOT NULL, ended TEXT);
CREATE TABLE IF NOT EXISTS messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id),
 turn_id TEXT NOT NULL REFERENCES turns(id), seq INTEGER NOT NULL, role TEXT NOT NULL,
 content TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL, UNIQUE(session_id,seq));
CREATE TABLE IF NOT EXISTS tool_runs(
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL REFERENCES turns(id), tool_call_id TEXT NOT NULL,
 name TEXT NOT NULL, arguments TEXT NOT NULL, state TEXT NOT NULL, result_json TEXT, started TEXT, ended TEXT,
 UNIQUE(turn_id,tool_call_id));
CREATE TABLE IF NOT EXISTS events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, turn_id TEXT, type TEXT NOT NULL, payload TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_calls(
 id TEXT PRIMARY KEY, turn_id TEXT, kind TEXT NOT NULL, model TEXT, status TEXT NOT NULL,
 usage TEXT, metadata TEXT, created TEXT NOT NULL, ended TEXT);
CREATE TABLE IF NOT EXISTS context_state(
 session_id TEXT PRIMARY KEY REFERENCES sessions(id), manifest TEXT NOT NULL DEFAULT '{}',
 working TEXT NOT NULL DEFAULT '{}', plan TEXT NOT NULL DEFAULT '{}', version INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS archives(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), message_ids TEXT NOT NULL,
 artifact TEXT NOT NULL, summary TEXT NOT NULL, tags TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memories(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, user_id TEXT NOT NULL, content TEXT NOT NULL,
 kind TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL, source_ids TEXT NOT NULL,
 created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots(
 id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, phase TEXT NOT NULL, state TEXT NOT NULL,
 commit_id TEXT, metadata TEXT NOT NULL, created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id,seq);
CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id,created);
CREATE INDEX IF NOT EXISTS events_turn ON events(turn_id,seq);
"""


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    """Only this worker accesses the SQLite connection; external I/O never holds a transaction."""

    def __init__(self, path: Path, workspace_id: str):
        self.path = path
        self.workspace_id = workspace_id
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kaki-store")
        self.db: sqlite3.Connection | None = None

    async def run(self, operation: str, *args, **kwargs):
        if operation.startswith("_"):
            raise ValueError("invalid store operation")
        method = getattr(self, "_" + operation)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, functools.partial(method, *args, **kwargs))

    async def open(self):
        await self.run("open")

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.db = sqlite3.connect(self.path, timeout=10)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise KakiError("状态数据库版本比当前Kaki新，请不要降级覆盖")
            self.db.executescript(SCHEMA)
            self.db.execute("PRAGMA user_version=1")
            self.db.commit()
            os.chmod(self.path, 0o600)
        except sqlite3.DatabaseError as exc:
            raise KakiError(
                f"状态数据库不可读写：{self.path}（{type(exc).__name__}）；没有重置历史"
            ) from None

    async def close(self):
        await self.run("close")
        self.executor.shutdown(wait=True)

    def _close(self):
        if self.db:
            self.db.close()
            self.db = None

    def _event(self, session: str, turn: str, kind: str, data: dict):
        self.db.execute(
            "INSERT INTO events(session_id,turn_id,type,payload,created) VALUES(?,?,?,?,?)",
            (session, turn, kind, dumps(data), now()),
        )

    def _new_session(self, title: str = "新会话") -> str:
        identity = uid("s_")
        with self.db:
            self.db.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?)", (identity, self.workspace_id, title, now(), now())
            )
        return identity

    def _sessions(self) -> list[dict]:
        return [
            dict(x)
            for x in self.db.execute(
                "SELECT * FROM sessions WHERE workspace_id=? ORDER BY updated DESC", (self.workspace_id,)
            )
        ]

    def _session(self, identity: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM sessions WHERE id=? AND workspace_id=?", (identity, self.workspace_id)
        ).fetchone()
        if not row:
            raise KakiError("当前项目中没有这个会话")
        return dict(row)

    def _session_models(self, session: str) -> list[str]:
        self._session(session)
        return [
            r[0]
            for r in self.db.execute(
                "SELECT DISTINCT m.model FROM model_calls m JOIN turns t ON t.id=m.turn_id "
                "WHERE t.session_id=? AND m.kind IN ('main','finalize') AND m.status='completed'",
                (session,),
            )
        ]

    def _append(self, session: str, turn: str, message: Message) -> Message:
        seq = self.db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE session_id=?", (session,)
        ).fetchone()[0]
        cur = self.db.execute(
            "INSERT INTO messages(session_id,turn_id,seq,role,content,payload,created) VALUES(?,?,?,?,?,?,?)",
            (session, turn, seq, message.role, message.content, dumps(message.payload()), message.created_at),
        )
        message.id, message.session_id, message.turn_id = cur.lastrowid, session, turn
        return message

    def _accept_turn(
        self, session: str, text: str, request_id: str | None = None
    ) -> tuple[str, Message, bool]:
        self._session(session)
        request_id = request_id or uid("r_")
        previous = self.db.execute("SELECT * FROM turns WHERE request_id=?", (request_id,)).fetchone()
        if previous:
            if previous["text"] != text or previous["session_id"] != session:
                raise KakiError("同一request_id不能提交不同任务")
            msg = next(m for m in self._messages(session) if m.turn_id == previous["id"] and m.role == "user")
            return previous["id"], msg, False
        turn = uid("t_")
        with self.db:
            self.db.execute(
                "INSERT INTO turns(id,session_id,request_id,status,text,created) VALUES(?,?,?,?,?,?)",
                (turn, session, request_id, "running", text, now()),
            )
            message = self._append(session, turn, Message("user", text))
            self.db.execute(
                "UPDATE sessions SET updated=?,title=CASE WHEN title='新会话' THEN ? ELSE title END WHERE id=?",
                (now(), text[:50], session),
            )
            self._event(session, turn, "turn.started", {"request_id": request_id, "message_id": message.id})
        return turn, message, True

    def _messages(self, session: str) -> list[Message]:
        self._session(session)
        result = []
        for row in self.db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY seq", (session,)):
            p = json.loads(row["payload"])
            result.append(
                Message(
                    **p, id=row["id"], session_id=session, turn_id=row["turn_id"], created_at=row["created"]
                )
            )
        return result

    def _commit_model(self, session: str, turn: str, response: ModelResponse) -> tuple[Message, list[dict]]:
        with self.db:
            self._assert_active(turn)
            msg = self._append(
                session,
                turn,
                Message(
                    "assistant",
                    response.text,
                    tool_calls=[asdict(c) for c in response.tool_calls],
                    native_items=response.native_items,
                ),
            )
            runs = []
            for call in response.tool_calls:
                run = {
                    "id": uid("run_"),
                    "tool_call_id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
                self.db.execute(
                    "INSERT INTO tool_runs(id,turn_id,tool_call_id,name,arguments,state) VALUES(?,?,?,?,?,?)",
                    (run["id"], turn, call.id, call.name, dumps(call.arguments), "planned"),
                )
                runs.append(run)
            self._event(
                session, turn, "model.committed", {"message_id": msg.id, "model_call_id": response.call_id}
            )
        return msg, runs

    def _assert_active(self, turn: str):
        row = self.db.execute("SELECT status FROM turns WHERE id=?", (turn,)).fetchone()
        if not row or row[0] in TERMINAL:
            raise KakiError("任务已结束或不存在，不能继续执行")

    def _mark_running(self, run: str):
        with self.db:
            row = self.db.execute("SELECT * FROM tool_runs WHERE id=?", (run,)).fetchone()
            if not row or row["state"] != "planned":
                raise KakiError("工具执行状态不允许自动重放")
            self._assert_active(row["turn_id"])
            self.db.execute("UPDATE tool_runs SET state='running',started=? WHERE id=?", (now(), run))

    def _commit_tool(self, session: str, turn: str, run: str, result: ToolResult) -> Message:
        with self.db:
            row = self.db.execute("SELECT * FROM tool_runs WHERE id=? AND turn_id=?", (run, turn)).fetchone()
            if not row or row["state"] not in ("running", "planned"):
                raise KakiError("工具结果已经提交或已标记未知")
            state = "failed" if result.failed else "succeeded"
            self.db.execute(
                "UPDATE tool_runs SET state=?,result_json=?,ended=? WHERE id=?",
                (state, dumps(asdict(result)), now(), run),
            )
            # The observation is data; it cannot acquire the priority of a user/system instruction.
            boundary = uid("data_")
            text = f"[{boundary}: untrusted tool data; source={row['name']}]\n{result.text}\n[/{boundary}]"
            msg = self._append(
                session,
                turn,
                Message(
                    "tool",
                    text,
                    tool_call_id=row["tool_call_id"],
                    parts=result.parts,
                    artifacts=result.artifacts,
                ),
            )
            self._event(session, turn, "tool.finished", {"run_id": run, "state": state, "message_id": msg.id})
        return msg

    def _draft(self, turn: str, text: str):
        with self.db:
            self.db.execute(
                "UPDATE turns SET result_json=? WHERE id=? AND status NOT IN ('completed','failed','cancelled','interrupted')",
                (dumps({"draft": text}), turn),
            )

    def _pending_observations(self, turn: str, *, reason: str):
        row = self.db.execute("SELECT session_id FROM turns WHERE id=?", (turn,)).fetchone()
        if not row:
            return
        for run in self.db.execute(
            "SELECT * FROM tool_runs WHERE turn_id=? AND state IN ('planned','running')", (turn,)
        ).fetchall():
            state = "unknown" if run["state"] == "running" else "cancelled"
            text = f"[Kaki恢复事实] 工具{run['name']}因{reason}中断；执行状态={state}。此记录不是成功结果。先核对现场，不要自动重放有副作用操作。"
            self.db.execute(
                "UPDATE tool_runs SET state=?,result_json=?,ended=? WHERE id=?",
                (state, dumps({"text": text, "failed": True}), now(), run["id"]),
            )
            self._append(row["session_id"], turn, Message("tool", text, tool_call_id=run["tool_call_id"]))

    def _finish_turn(self, result: TurnResult):
        with self.db:
            self._assert_active(result.turn_id)
            self._pending_observations(result.turn_id, reason=result.stop_reason)
            row = self.db.execute("SELECT session_id FROM turns WHERE id=?", (result.turn_id,)).fetchone()
            self.db.execute(
                "UPDATE turns SET status=?,stop_reason=?,error=?,result_json=?,before_snapshot=?,after_snapshot=?,ended=? WHERE id=?",
                (
                    result.status,
                    result.stop_reason,
                    result.error,
                    dumps(asdict(result)),
                    result.before_snapshot,
                    result.after_snapshot,
                    now(),
                    result.turn_id,
                ),
            )
            self.db.execute("UPDATE sessions SET updated=? WHERE id=?", (now(), row["session_id"]))
            self._event(row["session_id"], result.turn_id, "turn.finished", asdict(result))

    def _recover(self, session: str) -> list[dict]:
        self._session(session)
        recovered = []
        with self.db:
            rows = self.db.execute(
                "SELECT * FROM turns WHERE session_id=? AND status IN ('queued','running','waiting_user')",
                (session,),
            ).fetchall()
            for row in rows:
                self._pending_observations(row["id"], reason="上次进程退出")
                self.db.execute(
                    "UPDATE model_calls SET status='interrupted',ended=? WHERE turn_id=? AND status='running'",
                    (now(), row["id"]),
                )
                self.db.execute(
                    "UPDATE turns SET status='interrupted',stop_reason='process_exit',ended=? WHERE id=?",
                    (now(), row["id"]),
                )
                self._event(session, row["id"], "turn.recovered", {"reason": "process_exit"})
                recovered.append(dict(row))
        return recovered

    def _turns(self, session: str) -> list[dict]:
        self._session(session)
        return [
            dict(r)
            for r in self.db.execute("SELECT * FROM turns WHERE session_id=? ORDER BY created", (session,))
        ]

    def _tool_runs(self, turn: str) -> list[dict]:
        return [
            dict(r)
            for r in self.db.execute("SELECT * FROM tool_runs WHERE turn_id=? ORDER BY rowid", (turn,))
        ]

    def _question(self, turn: str, question: dict | None):
        with self.db:
            self._assert_active(turn)
            self.db.execute(
                "UPDATE turns SET status=? WHERE id=?", ("waiting_user" if question else "running", turn)
            )
            self._event("", turn, "question.requested" if question else "question.answered", question or {})

    def _call_record(self, record: dict):
        with self.db:
            self.db.execute(
                "INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,usage=excluded.usage,metadata=excluded.metadata,ended=excluded.ended",
                (
                    record["id"],
                    record.get("turn_id"),
                    record["kind"],
                    record["model"],
                    record["status"],
                    dumps(record.get("usage", {})),
                    dumps(record.get("metadata", {})),
                    record.get("created", now()),
                    record.get("ended"),
                ),
            )

    def _usage(self, turn: str) -> dict:
        summary = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
            "unknown_calls": 0,
            "by_kind": {},
        }
        for row in self.db.execute("SELECT * FROM model_calls WHERE turn_id=?", (turn,)):
            usage = json.loads(row["usage"] or "{}")
            group = summary["by_kind"].setdefault(row["kind"], {"calls": 0, "total_tokens": 0})
            summary["calls"] += 1
            group["calls"] += 1
            if not usage:
                summary["unknown_calls"] += 1
            for key in ["prompt_tokens", "completion_tokens", "total_tokens"]:
                summary[key] += int(usage.get(key, 0) or 0)
            group["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
        return summary

    def _context_get(self, session: str) -> dict:
        row = self.db.execute("SELECT * FROM context_state WHERE session_id=?", (session,)).fetchone()
        return (
            {
                "manifest": json.loads(row["manifest"]),
                "working": json.loads(row["working"]),
                "plan": json.loads(row["plan"]),
                "version": row["version"],
            }
            if row
            else {"manifest": {}, "working": {}, "plan": {}, "version": 0}
        )

    def _context_set(
        self,
        session: str,
        manifest: dict | None = None,
        working: dict | None = None,
        plan: dict | None = None,
    ):
        old = self._context_get(session)
        with self.db:
            self.db.execute(
                "INSERT INTO context_state VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET manifest=excluded.manifest,working=excluded.working,plan=excluded.plan,version=excluded.version",
                (
                    session,
                    dumps(old["manifest"] if manifest is None else manifest),
                    dumps(old["working"] if working is None else working),
                    dumps(old["plan"] if plan is None else plan),
                    old["version"] + 1,
                ),
            )

    def _archive_add(
        self, session: str, ids: list[int], artifact: dict, summary: str, tags: list[str]
    ) -> str:
        identity = uid("archive_")
        with self.db:
            self.db.execute(
                "INSERT INTO archives VALUES(?,?,?,?,?,?,?)",
                (identity, session, dumps(ids), dumps(artifact), summary, dumps(tags), now()),
            )
        return identity

    def _archive_get(self, session: str, identity: str) -> dict:
        r = self.db.execute(
            "SELECT * FROM archives WHERE session_id=? AND id=?", (session, identity)
        ).fetchone()
        if not r:
            raise KakiError("归档不存在或不属于当前会话")
        return {**dict(r), "message_ids": json.loads(r["message_ids"]), "artifact": json.loads(r["artifact"])}

    def _memories(self, user_id: str) -> list[dict]:
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM memories WHERE workspace_id=? AND user_id=? AND state='active' ORDER BY updated DESC",
                (self.workspace_id, user_id),
            )
        ]

    def _remember(
        self, user_id: str, content: str, kind: str, source_ids: list[int], replaces: str | None = None
    ) -> str:
        identity = uid("m_")
        version = 1
        with self.db:
            if replaces:
                r = self.db.execute(
                    "SELECT * FROM memories WHERE id=? AND workspace_id=? AND user_id=?",
                    (replaces, self.workspace_id, user_id),
                ).fetchone()
                if not r:
                    raise KakiError("待替换记忆不存在")
                version = r["version"] + 1
                self.db.execute(
                    "UPDATE memories SET state='superseded',updated=? WHERE id=?", (now(), replaces)
                )
            self.db.execute(
                "INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    self.workspace_id,
                    user_id,
                    content,
                    kind,
                    "active",
                    version,
                    dumps(source_ids),
                    now(),
                    now(),
                ),
            )
        return identity

    def _retract(self, user_id: str, identity: str):
        with self.db:
            r = self.db.execute(
                "UPDATE memories SET state='retracted',updated=? WHERE id=? AND user_id=? AND workspace_id=?",
                (now(), identity, user_id, self.workspace_id),
            )
            if not r.rowcount:
                raise KakiError("记忆不存在")

    def _snapshot(self, identity: str, turn: str, phase: str, state: str, commit: str | None, metadata: dict):
        with self.db:
            self.db.execute(
                "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,commit_id=excluded.commit_id,metadata=excluded.metadata",
                (identity, turn, phase, state, commit, dumps(metadata), now()),
            )

    def _snapshots(self, turn: str | None = None) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM snapshots" + (" WHERE turn_id=?" if turn else "") + " ORDER BY created",
            (turn,) if turn else (),
        )
        return [{**dict(r), "metadata": json.loads(r["metadata"])} for r in rows]
