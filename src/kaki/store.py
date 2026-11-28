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

# SQL 表定义是真实协议数据，注释不改字符串内容；各表职责见 core.md。
# sessions/turns 管作用域与幂等，messages/tool_runs 保存不可替代的执行事实。
# events/model_calls 提供审计与用量，context_state/archives/memories 保存派生选择与版本。
# snapshots/decisions 记录恢复和交互；SQLite 是嵌入文件，不需要启动数据库服务。
SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, title TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
 archived INTEGER NOT NULL DEFAULT 0);
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
CREATE TABLE IF NOT EXISTS decisions(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 turn_id TEXT NOT NULL REFERENCES turns(id), kind TEXT NOT NULL, state TEXT NOT NULL,
 question TEXT NOT NULL, options TEXT NOT NULL, operation TEXT NOT NULL,
 response TEXT, created TEXT NOT NULL, ended TEXT);
CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id,seq);
CREATE INDEX IF NOT EXISTS turns_session ON turns(session_id,created);
CREATE INDEX IF NOT EXISTS events_turn ON events(turn_id,seq);
"""


def dumps(value: Any) -> str:
    # 统一持久 JSON 编码，不把原生项改造成自定义模型文本。
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    # 持久事实的唯一访问层；每个实例由一个专用线程独享 SQLite 连接，不是本机数据库服务。
    """Only this worker accesses the SQLite connection; external I/O never holds a transaction."""

    def __init__(self, path: Path, workspace_id: str):
        # 这里只分配连接所有权；真正打开、迁移与恢复仍在 Store.run 的线程中执行。
        self.path = path
        self.workspace_id = workspace_id
        # 单 worker 保证 SQLite 连接线程归属，数据库 I/O 不占主事件循环。
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kaki-store")
        # 仅在上述 worker 内访问；进程关闭后不复用该连接。
        self.db: sqlite3.Connection | None = None

    async def run(self, operation: str, *args, **kwargs):
        # 把短数据库操作排入唯一 worker，避免在 asyncio/UI 线程同步阻塞或跨线程共享连接。
        if operation.startswith("_"):
            raise ValueError("invalid store operation")
        method = getattr(self, "_" + operation)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, functools.partial(method, *args, **kwargs))

    async def open(self):
        # 所有打开/迁移动作也通过同一连接 owner 执行。
        await self.run("open")

    def _open(self):
        # 设置 WAL/完整同步和外键，必要时先一致备份再迁移；拒绝未知的新 schema。
        if self.db is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.db = sqlite3.connect(self.path, timeout=10)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > 3:
                raise KakiError("状态数据库版本比当前Kaki新，请不要降级覆盖")
            # Preserve a SQLite-consistent copy, including WAL data, before adding App metadata.
            if version in (1, 2):
                backup = self.path.with_name(self.path.name + f".v{version}-" + uid()[:12] + ".backup")
                descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
                with sqlite3.connect(backup) as connection:
                    self.db.backup(connection)
            self.db.executescript(SCHEMA)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}
            if "archived" not in columns:
                self.db.execute("ALTER TABLE sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
            self.db.execute("PRAGMA user_version=3")
            self.db.commit()
            os.chmod(self.path, 0o600)
        except sqlite3.DatabaseError as exc:
            raise KakiError(
                f"状态数据库不可读写：{self.path}（{type(exc).__name__}）；没有重置历史"
            ) from None

    async def close(self):
        # 先关闭该线程的连接，再结束执行器；关闭后的 Store 实例不能重新使用。
        await self.run("close")
        self.executor.shutdown(wait=True)

    def _close(self):
        # 提交并关闭连接；异步调用方随后关闭单 worker 执行器。
        if self.db:
            self.db.close()
            self.db = None

    def _event(self, session: str, turn: str, kind: str, data: dict):
        # 与业务写事务一同记录关键事件；不同于 Runtime.emit 的全部实时 UI 事件。
        self.db.execute(
            "INSERT INTO events(session_id,turn_id,type,payload,created) VALUES(?,?,?,?,?)",
            (session, turn, kind, dumps(data), now()),
        )

    def _new_session(self, title: str = "新会话") -> str:
        # 显式写会话列，避免以后加列破坏 INSERT 位置；会话不创建任何执行循环。
        identity = uid("s_")
        with self.db:
            self.db.execute(
                "INSERT INTO sessions(id,workspace_id,title,created,updated) VALUES(?,?,?,?,?)",
                (identity, self.workspace_id, title, now(), now()),
            )
        return identity

    def _sessions(self, archived: bool = False) -> list[dict]:
        # 项目内按更新时间查询会话，软归档与正常列表分开。
        return [
            dict(x)
            for x in self.db.execute(
                "SELECT * FROM sessions WHERE workspace_id=? AND archived=? ORDER BY updated DESC",
                (self.workspace_id, int(archived)),
            )
        ]

    def _session_summaries(self, archived: bool = False) -> list[dict]:
        # 组合侧边栏状态与预览，供导航使用而非替代逐条执行证据。
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT s.*, (SELECT status FROM turns WHERE session_id=s.id ORDER BY created DESC LIMIT 1) AS status, "
                "(SELECT text FROM turns WHERE session_id=s.id ORDER BY created DESC LIMIT 1) AS preview "
                "FROM sessions s WHERE workspace_id=? AND archived=? ORDER BY updated DESC",
                (self.workspace_id, int(archived)),
            )
        ]

    def _rename_session(self, session: str, title: str):
        # 标题仅是导航信息；校验长度/控制字符后更新，不改原始任务内容。
        self._session(session)
        title = title.strip()
        if not title or len(title) > 80 or any(ord(char) < 32 for char in title):
            raise KakiError("会话标题需要1–80个可见字符")
        with self.db:
            self.db.execute("UPDATE sessions SET title=?,updated=? WHERE id=?", (title, now(), session))

    def _archive_session(self, session: str, archived: bool):
        # 软归档保留消息、工具与快照；活跃任务的会话不能归档。
        self._session(session)
        if self.db.execute(
            "SELECT 1 FROM turns WHERE session_id=? AND status IN ('queued','running','waiting_user')",
            (session,),
        ).fetchone():
            raise KakiError("任务仍在运行，结束或停止后才能归档")
        with self.db:
            self.db.execute(
                "UPDATE sessions SET archived=?,updated=? WHERE id=?", (int(archived), now(), session)
            )

    def _session(self, identity: str) -> dict:
        # 每次按 workspace_id 校验归属，避免一个 ID 读取别的项目。
        row = self.db.execute(
            "SELECT * FROM sessions WHERE id=? AND workspace_id=?", (identity, self.workspace_id)
        ).fetchone()
        if not row:
            raise KakiError("当前项目中没有这个会话")
        return dict(row)

    def _session_models(self, session: str) -> list[str]:
        # 只用 main/finalize 校验恢复主模型，理解模型不能改变主会话约束。
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
        # 赋稳定数据库 ID 与会话内序号；payload 不混入这些存储层标识。
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
        # 同一事务完成请求幂等与用户消息接收；重复 ID 不会再跑一次副作用。
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
        # 恢复原始领域消息，并从调用日志派生旧记录的 token 预留；不重写历史 JSON。
        self._session(session)
        # Derive accounting for legacy messages from their immutable call/event
        # records; never rewrite the original transcript merely to resume it.
        reserves = {}
        for row in self.db.execute(
            "SELECT e.payload,c.usage FROM events e JOIN model_calls c "
            "ON c.id=json_extract(e.payload,'$.model_call_id') "
            "WHERE e.session_id=? AND e.type='model.committed'",
            (session,),
        ):
            usage = json.loads(row["usage"] or "{}")
            value = usage.get("completion_tokens")
            if type(value) is int and value > 0:
                reserves[json.loads(row["payload"])["message_id"]] = max(
                    value, usage.get("reasoning_tokens", 0)
                )
        result = []
        for row in self.db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY seq", (session,)):
            p = json.loads(row["payload"])
            if p.get("native_token_reserve") is None and row["id"] in reserves:
                p["native_token_reserve"] = reserves[row["id"]]
            result.append(
                Message(
                    **p, id=row["id"], session_id=session, turn_id=row["turn_id"], created_at=row["created"]
                )
            )
        return result

    def _commit_model(self, session: str, turn: str, response: ModelResponse) -> tuple[Message, list[dict]]:
        # 把完整 assistant 输出和所有 planned tool_runs 原子提交，确保先有意图再执行工具。
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
                    native_token_reserve=(
                        max(response.usage["completion_tokens"], response.usage.get("reasoning_tokens", 0))
                        if response.usage.get("completion_tokens", 0) > 0
                        else None
                    ),
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
        # 迟到回调不能继续向终态 Turn 追加工具副作用。
        row = self.db.execute("SELECT status FROM turns WHERE id=?", (turn,)).fetchone()
        if not row or row[0] in TERMINAL:
            raise KakiError("任务已结束或不存在，不能继续执行")

    def _mark_running(self, run: str):
        # 实际 I/O 前提交状态；崩溃后 running 只能判未知，不能当未执行重试。
        with self.db:
            row = self.db.execute("SELECT * FROM tool_runs WHERE id=?", (run,)).fetchone()
            if not row or row["state"] != "planned":
                raise KakiError("工具执行状态不允许自动重放")
            self._assert_active(row["turn_id"])
            self.db.execute("UPDATE tool_runs SET state='running',started=? WHERE id=?", (now(), run))

    def _commit_tool(self, session: str, turn: str, run: str, result: ToolResult) -> Message:
        # 结果与配对 tool 消息同事务写入；边界标记防止工具文本获得用户指令优先级。
        with self.db:
            row = self.db.execute("SELECT * FROM tool_runs WHERE id=? AND turn_id=?", (run, turn)).fetchone()
            if not row or row["state"] not in ("running", "planned"):
                raise KakiError("工具结果已经提交或已标记未知")
            state = (
                "unknown"
                if result.uncertain
                else "denied"
                if result.denied
                else "failed"
                if result.failed
                else "succeeded"
            )
            if result.denied and (result.executed or row["state"] != "planned"):
                raise KakiError("拒绝执行必须发生在工具启动之前")
            self.db.execute(
                "UPDATE tool_runs SET state=?,result_json=?,ended=? WHERE id=?",
                (state, dumps(asdict(result)), now(), run),
            )
            # The observation is data; it cannot acquire the priority of a user/system instruction.
            # 工具/网页内容是低权限数据，显式标出来源，不能伪装成新的用户或系统批准。
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
        # 草稿仅为恢复诊断；没有上游完整终事件时不得当作可执行模型输出。
        with self.db:
            self.db.execute(
                "UPDATE turns SET result_json=? WHERE id=? AND status NOT IN ('completed','failed','cancelled','interrupted')",
                (dumps({"draft": text}), turn),
            )

    def _pending_observations(self, turn: str, *, reason: str):
        # 补齐被取消/未知的工具结果，关闭完整协议组，避免恢复后出现孤立调用。
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
        # 终态写入点；已结束 Turn 不允许后续回调二次覆盖。
        with self.db:
            self._assert_active(result.turn_id)
            self._pending_observations(result.turn_id, reason=result.stop_reason)
            self.db.execute(
                "UPDATE decisions SET state='expired',ended=? WHERE turn_id=? AND state='pending'",
                (now(), result.turn_id),
            )
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
        # 租约允许时协调崩溃残留：planned 取消、running 未知、交互过期；从不重放命令。
        self._session(session)
        recovered = []
        with self.db:
            rows = self.db.execute(
                "SELECT * FROM turns WHERE session_id=? AND status IN ('queued','running','waiting_user')",
                (session,),
            ).fetchall()
            for row in rows:
                self.db.execute(
                    "UPDATE decisions SET state='expired',ended=? WHERE turn_id=? AND state='pending'",
                    (now(), row["id"]),
                )
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
        # 读取历史轮次，保留真实结束原因，不把一次最终回答推断为业务成功。
        self._session(session)
        return [
            dict(r)
            for r in self.db.execute("SELECT * FROM turns WHERE session_id=? ORDER BY created", (session,))
        ]

    def _tool_runs(self, turn: str) -> list[dict]:
        # 返回本轮逐项执行日志；Turn 统计可以据此重建，避免后续模型异常清零事实。
        return [
            dict(r)
            for r in self.db.execute("SELECT * FROM tool_runs WHERE turn_id=? ORDER BY rowid", (turn,))
        ]

    def _question(self, turn: str, question: dict | None):
        # 保存给模型看的普通任务问答，与独立审批 ledger 的语义不同。
        with self.db:
            self._assert_active(turn)
            self.db.execute(
                "UPDATE turns SET status=? WHERE id=?", ("waiting_user" if question else "running", turn)
            )
            self._event("", turn, "question.requested" if question else "question.answered", question or {})

    def _decision_create(self, identity, session, turn, kind, question, options, operation):
        # 先持久化问题与作用域，再把 Turn 设为 waiting_user，供重连与审计使用。
        with self.db:
            self._assert_active(turn)
            if self.db.execute(
                "SELECT 1 FROM decisions WHERE turn_id=? AND state='pending'", (turn,)
            ).fetchone():
                raise KakiError("已有未回答的问题")
            self.db.execute(
                "INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    session,
                    turn,
                    kind,
                    "pending",
                    question,
                    dumps(options),
                    dumps(operation),
                    None,
                    now(),
                    None,
                ),
            )
            self.db.execute("UPDATE turns SET status='waiting_user' WHERE id=?", (turn,))
            self._event(session, turn, "question.requested", {"question_id": identity, "kind": kind})

    def _decision_answer(self, identity, session, turn, response):
        # 校验问题仍有效后提交回答；澄清会追加真实 user 消息，授权不会伪造任务原文。
        with self.db:
            self._assert_active(turn)
            row = self.db.execute(
                "SELECT * FROM decisions WHERE id=? AND session_id=? AND turn_id=?", (identity, session, turn)
            ).fetchone()
            if not row or row["state"] != "pending":
                raise KakiError("问题已过期或已经回答")
            self.db.execute(
                "UPDATE decisions SET state='answered',response=?,ended=? WHERE id=?",
                (dumps(response), now(), identity),
            )
            self.db.execute("UPDATE turns SET status='running' WHERE id=?", (turn,))
            if row["kind"] == "clarification":
                message = self._append(session, turn, Message("user", response["text"]))
                response = {**response, "message_id": message.id}
            self._event(session, turn, "question.answered", {"question_id": identity, **response})
            return response

    def _decision_expire(self, identity):
        # 取消/结束后问题只失效不删除，旧窗口不能拿过期 ID 解锁新任务。
        with self.db:
            self.db.execute(
                "UPDATE decisions SET state='expired',ended=? WHERE id=? AND state='pending'",
                (now(), identity),
            )

    def _decisions(self, turn):
        # 保留本轮交互事实供 UI/审计，恢复时不会自动重新批准。
        return [
            dict(r)
            for r in self.db.execute("SELECT * FROM decisions WHERE turn_id=? ORDER BY rowid", (turn,))
        ]

    def _trace(self, session, turn, kind, data):
        # 只记录派生分析诊断，不能把它当网络调用实际完成证明。
        with self.db:
            self._assert_active(turn)
            self._event(session, turn, kind, data)

    def _call_record(self, record: dict):
        # 按本地调用 ID 更新一次真实网络尝试；重试是不同 ID，不覆盖已知用量。
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
        # 累计所有已知调用；缺失 usage 计入 unknown_calls，不能伪装成零成本成功。
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
        # 读取可重建的窗口状态，不是第二份原始会话事实。
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
        # 合并 manifest/working/plan 并递增版本；原始消息内容始终不在这里改写。
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
        # 归档引用带所属 Session 与稳定消息 ID，防止跨会话误召回。
        identity = uid("archive_")
        with self.db:
            self.db.execute(
                "INSERT INTO archives VALUES(?,?,?,?,?,?,?)",
                (identity, session, dumps(ids), dumps(artifact), summary, dumps(tags), now()),
            )
        return identity

    def _archive_get(self, session: str, identity: str) -> dict:
        # 读取引用前验证会话归属；文件内容 hash 由 Archive 层再检查。
        r = self.db.execute(
            "SELECT * FROM archives WHERE session_id=? AND id=?", (session, identity)
        ).fetchone()
        if not r:
            raise KakiError("归档不存在或不属于当前会话")
        return {**dict(r), "message_ids": json.loads(r["message_ids"]), "artifact": json.loads(r["artifact"])}

    def _memories(self, user_id: str) -> list[dict]:
        # 召回只允许当前项目/用户的 active 记忆版本。
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
        # 显式记忆产生新版本，旧版本保留；不把模型猜测自动升级为长期事实。
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
        # 撤回记忆只改变可检索状态，保留审计历史。
        with self.db:
            r = self.db.execute(
                "UPDATE memories SET state='retracted',updated=? WHERE id=? AND user_id=? AND workspace_id=?",
                (now(), identity, user_id, self.workspace_id),
            )
            if not r.rowcount:
                raise KakiError("记忆不存在")

    def _snapshot(self, identity: str, turn: str, phase: str, state: str, commit: str | None, metadata: dict):
        # 登记快照的 creating/ready/failed；只有 ready 的 commit 才能用于恢复。
        with self.db:
            self.db.execute(
                "INSERT INTO snapshots VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,commit_id=excluded.commit_id,metadata=excluded.metadata",
                (identity, turn, phase, state, commit, dumps(metadata), now()),
            )

    def _snapshots(self, turn: str | None = None) -> list[dict]:
        # 查询记录中的 ready/failed 状态，不能仅凭存在一个 commit 字符串判断可恢复。
        rows = self.db.execute(
            "SELECT * FROM snapshots" + (" WHERE turn_id=?" if turn else "") + " ORDER BY created",
            (turn,) if turn else (),
        )
        return [{**dict(r), "metadata": json.loads(r["metadata"])} for r in rows]
