"""Local application owner: one Turn, one workspace lease, one terminal event."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import re

from PIL import Image

from kaki.browser_mcp import BrowserMcp
from kaki.checkpoint import Checkpoint
from kaki.config import Config, safe_error
from kaki.context import ContextEngine
from kaki.loop import AgentLoop
from kaki.provider import RightCodeProvider
from kaki.recovery import Recovery
from kaki.store import Store
from kaki.tools import Tool, ToolRegistry, integer, string
from kaki.types import Event, KakiError, TurnResult, uid
from kaki.workspace import Workspace, WorkspaceLease


class Runtime:
    def __init__(self, workspace: Workspace, config: Config, *, provider=None, interactive: bool = True):
        self.workspace, self.config = workspace, config
        self.interactive = interactive
        self.store = Store(workspace.state / "kaki.sqlite3", workspace.id)
        self.provider = provider
        self.browser = BrowserMcp(workspace, config)
        self.context = None
        self.tools = None
        self.checkpoint = Checkpoint(workspace, self.store, config)
        self.recovery = Recovery(self.store)
        self.lease = WorkspaceLease(workspace.state)
        self.session_id = ""
        self.active = None
        self.active_turn = ""
        self.partial_text = ""
        self.listeners = []
        self.question = None
        self.initialized = False
        self.closed = False
        self.recovery_plan = {}
        self.browser_warning = None

    async def emit(self, kind: str, turn: str = "", data: dict | None = None):
        event = Event(kind, turn, data or {})
        for listener in self.listeners:
            await listener(event)

    def image_part(self, part: dict) -> dict:
        path = self.workspace.artifact_path(part["artifact_id"])
        image = Image.open(path)
        image.thumbnail((1568, 1568))
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        return {
            "type": "input_image",
            "image_url": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode(),
            "detail": "auto",
        }

    async def initialize(self, resume: str | None = None):
        if self.initialized:
            return
        await self.store.open()
        if self.provider is None:
            self.provider = RightCodeProvider(
                self.config, lambda record: self.store.run("call_record", record), self.image_part
            )
        self.context = ContextEngine(self.workspace, self.store, self.config, self.provider)
        self.tools = ToolRegistry(self.workspace, self.config, self.context, self.browser, self.ask_user)
        self.tools.add(
            Tool(
                "kaki_artifact_read",
                "Read full stored text tool output by artifact_id with exact character pagination.",
                {"artifact_id": string(), "offset": integer(), "limit": integer("characters", 100, 16000)},
                ["artifact_id"],
                "READ",
                self.tools.artifact_read,
                True,
            )
        )
        sessions = await self.store.run("sessions")
        if resume:
            if resume == "last":
                if not sessions:
                    raise KakiError("当前项目还没有可恢复会话")
                self.session_id = sessions[0]["id"]
            else:
                matches = [s for s in sessions if s["id"] == resume or s["id"].startswith(resume)]
                if len(matches) != 1:
                    raise KakiError("会话不存在或前缀不唯一")
                self.session_id = matches[0]["id"]
            await self.check_session_model(self.session_id)
            try:
                self.lease.acquire()
            except KakiError:
                self.recovery_plan = {"items": [], "notice": "工作区仍有其他活跃任务，未执行恢复改写"}
            else:
                try:
                    self.recovery_plan = await self.recovery.reconcile(self.session_id)
                finally:
                    self.lease.release()
        else:
            self.session_id = await self.store.run("new_session")
        self.context.last_metadata = (await self.store.run("context_get", self.session_id))["plan"]
        if self.browser.enabled:
            await self.emit("runtime.status", data={"text": "正在连接Playwright MCP…"})
            try:
                await self.tools.connect_browser()
            except Exception as exc:
                self.browser_warning = safe_error(exc)
        self.initialized = True
        await self.emit(
            "runtime.ready",
            data={
                "session_id": self.session_id,
                "browser_tools": len(self.browser.tools),
                "browser_warning": self.browser_warning,
                "recovery": self.recovery_plan,
            },
        )

    async def new_session(self, title="新会话"):
        if self.active and not self.active.done():
            raise KakiError("请先取消当前任务")
        self.session_id = await self.store.run("new_session", title)
        self.recovery_plan = {}
        return self.session_id

    async def switch_session(self, identity: str):
        if self.active and not self.active.done():
            raise KakiError("请先结束或取消当前任务")
        sessions = await self.store.run("sessions")
        matches = [s for s in sessions if s["id"] == identity or s["id"].startswith(identity)]
        if identity == "last" and sessions:
            matches = [sessions[0]]
        if len(matches) != 1:
            raise KakiError("会话不存在或前缀不唯一")
        await self.check_session_model(matches[0]["id"])
        self.lease.acquire()
        try:
            self.session_id = matches[0]["id"]
            self.recovery_plan = await self.recovery.reconcile(self.session_id)
            self.context.last_metadata = (await self.store.run("context_get", self.session_id))["plan"]
        finally:
            self.lease.release()
        return self.session_id

    async def check_session_model(self, session: str):
        models = await self.store.run("session_models", session)
        current = self.config.agents.defaults.model.removeprefix("openai/")
        if any(model.removeprefix("openai/") != current for model in models):
            raise KakiError(
                "该会话的模型与当前配置不同，原生reasoning项不能保证跨模型重放；请使用原模型恢复或新建会话"
            )

    @property
    def snapshots_enabled(self):
        policy = self.config.runtime.checkpoint.policy
        return policy == "always" or policy == "interactive" and self.interactive

    def submit(self, text: str, request_id: str | None = None) -> asyncio.Task:
        if not self.initialized:
            raise KakiError("Runtime还未就绪")
        if self.active and not self.active.done():
            raise KakiError("当前任务仍在运行；可以取消或回答待确认问题")
        if not text.strip():
            raise KakiError("任务不能为空")
        self.active = asyncio.create_task(self._turn(text, request_id), name="kaki-user-turn")
        return self.active

    async def run(self, text: str, request_id: str | None = None) -> TurnResult:
        return await self.submit(text, request_id)

    async def _turn(self, text: str, request_id: str | None) -> TurnResult:
        turn = ""
        result = None
        user = None
        warnings = []
        before = None
        after = None
        self.partial_text = ""
        self.lease.acquire()
        try:
            accepted, user, created = await self.store.run("accept_turn", self.session_id, text, request_id)
            if not created:
                rows = await self.store.run("turns", self.session_id)
                previous = next(row for row in rows if row["id"] == accepted)
                if previous["status"] not in {"completed", "failed", "interrupted", "cancelled"}:
                    raise KakiError("此请求已经被接受且尚未结束；不重复执行")
                import json

                data = json.loads(previous["result_json"] or "{}")
                if "turn_id" not in data:
                    raise KakiError("此请求已中断；请查看恢复信息并提交新请求")
                await self.emit("request.duplicate", accepted, {"request_id": request_id})
                return TurnResult(**data)
            turn = accepted
            self.active_turn = turn
            await self.emit(
                "turn.started", turn, {"session_id": self.session_id, "message_id": user.id, "text": text}
            )
            if self.snapshots_enabled:
                await self.emit("runtime.status", turn, {"text": "建立工作区基线快照…"})
                snapshot = await self.checkpoint.capture(turn, "before")
                before = snapshot["id"]
                if snapshot["state"] != "ready":
                    warnings.append(snapshot.get("error", "快照失败"))
                    await self.emit("snapshot.warning", turn, snapshot)
            history = [m for m in await self.store.run("messages", self.session_id) if m.id != user.id]
            if text.strip() == "/compact":
                window = await self.context.assemble(
                    self.session_id,
                    history,
                    user,
                    self.tools.definitions(),
                    force=True,
                    recovery=self.recovery.context(self.recovery_plan),
                )
                from .types import ModelResponse

                answer = f"上下文已整理：路径={window.metadata['path']}，保留历史{len(window.metadata['included_ids'])}条，估算输入{window.estimated_tokens}/{window.budget.input_limit} tokens。原始历史和归档仍保留。"
                message, _ = await self.store.run(
                    "commit_model", self.session_id, turn, ModelResponse(text=answer)
                )
                await self.emit(
                    "model.committed", turn, {"draft_id": "", "message_id": message.id, "text": answer}
                )
                result = TurnResult(turn, "completed", answer, "manual_compaction")
            else:
                result = await AgentLoop(self).run(
                    turn, self.session_id, user, history, self.recovery.context(self.recovery_plan)
                )
        except asyncio.CancelledError:
            if not turn:
                raise
            result = TurnResult(
                turn,
                "cancelled",
                self.partial_text or "已取消当前任务；已完成副作用不会自动撤回。",
                "user_cancel",
            )
        except Exception as exc:
            if not turn:
                raise
            result = TurnResult(
                turn,
                "failed",
                self.partial_text or "任务执行失败：" + safe_error(exc),
                getattr(exc, "category", "execution_error"),
                error=safe_error(exc),
            )
        finally:
            try:
                if result and turn:
                    if self.question and not self.question["future"].done():
                        self.question["future"].cancel()
                    self.question = None
                    if self.snapshots_enabled:
                        try:
                            snapshot = await asyncio.shield(self.checkpoint.capture(turn, "after"))
                            after = snapshot["id"]
                            if snapshot["state"] != "ready":
                                warnings.append(snapshot.get("error", "结束快照失败"))
                                await self.emit("snapshot.warning", turn, snapshot)
                        except Exception as exc:
                            warnings.append("结束快照失败：" + safe_error(exc))
                    result.before_snapshot = before
                    result.after_snapshot = after
                    result.usage = await self.store.run("usage", turn)
                    try:
                        await self.context.after_turn(self.session_id, user, result)
                    except Exception as exc:
                        warnings.append("派生上下文更新失败，原始执行事实仍保留：" + safe_error(exc))
                    await self.store.run("finish_turn", result)
                    await self.emit("turn.finished", turn, as_result(result, warnings))
                    self.recovery_plan = {}
            finally:
                self.active_turn = ""
                self.lease.release()
        return result

    async def ask_user(self, question: str, options: list[str]) -> str:
        if self.question:
            raise KakiError("已有未回答的问题")
        future = asyncio.get_running_loop().create_future()
        identity = uid("q_")
        self.question = {"id": identity, "future": future, "question": question, "options": options}
        await self.store.run(
            "question", self.active_turn, {"question_id": identity, "question": question, "options": options}
        )
        await self.emit(
            "question.requested",
            self.active_turn,
            {"question_id": identity, "question": question, "options": options},
        )
        try:
            return await future
        finally:
            self.question = None
            await self.store.run("question", self.active_turn, None)

    async def answer(self, question_id: str, text: str):
        if not self.question or self.question["id"] != question_id:
            raise KakiError("问题已过期或不属于当前任务")
        self.question["future"].set_result(text)

    async def cancel(self):
        if self.active and not self.active.done():
            self.active.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.active

    async def close(self):
        if self.closed:
            return
        self.closed = True
        await self.cancel()
        await self.browser.close()
        if self.provider and hasattr(self.provider, "close"):
            await self.provider.close()
        await self.store.close()

    @staticmethod
    def is_test_command(command: str) -> bool:
        return bool(
            re.search(
                r"(?:^|[;&|]\s*|\s)(?:pytest|python(?:3)?\s+-m\s+(?:unittest|pytest)|make\s+(?:docker-)?test|go\s+test|npm\s+(?:run\s+)?test|cargo\s+test)(?:\s|$)",
                command,
            )
        )


def as_result(result: TurnResult, warnings: list[str]) -> dict:
    from dataclasses import asdict

    return {
        **asdict(result),
        "warnings": warnings,
        "recovery_available": bool(result.before_snapshot and result.after_snapshot and not warnings),
    }
