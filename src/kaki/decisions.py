"""One durable, scoped human decision at a time; model text cannot approve it."""

from __future__ import annotations

import asyncio

from kaki.types import InteractionRequired, KakiError, uid

APPROVAL_LABELS = {"once": "仅允许这一次", "turn": "本轮允许同类操作", "deny": "拒绝执行"}


class DecisionService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.answered: dict[str, dict] = {}

    async def request(self, kind, question, options=None, operation=None):
        rt = self.runtime
        if not rt.interactive:
            raise InteractionRequired(kind, question)
        if rt.question:
            raise KakiError("已有未回答的问题")
        identity = uid("q_")
        options = options or []
        operation = operation or {}
        future = asyncio.get_running_loop().create_future()
        await rt.store.run(
            "decision_create", identity, rt.session_id, rt.active_turn, kind, question, options, operation
        )
        rt.question = {
            "id": identity,
            "future": future,
            "kind": kind,
            "question": question,
            "options": options,
            "operation": operation,
            "session_id": rt.session_id,
            "turn_id": rt.active_turn,
        }
        try:
            await rt.emit(
                "question.requested",
                rt.active_turn,
                {
                    "question_id": identity,
                    "kind": kind,
                    "question": question,
                    "options": options,
                    "operation": operation,
                },
            )
            return await future
        finally:
            rt.question = None
            await rt.store.run("decision_expire", identity)

    async def answer(self, identity, text, choice=None):
        rt = self.runtime
        q = rt.question
        # Idempotent duplicate delivery is accepted only in the same active scope.
        previous = self.answered.get(identity)
        if previous and previous["turn_id"] == rt.active_turn and previous["session_id"] == rt.session_id:
            if previous["text"] == text and previous["choice"] == choice:
                return
            raise KakiError("此问题已回答，不能覆盖")
        if (
            not q
            or q["id"] != identity
            or q["session_id"] != rt.session_id
            or q["turn_id"] != rt.active_turn
            or q["future"].done()
        ):
            raise KakiError("问题已过期或不属于当前任务")
        if not text.strip() or len(text) > 100000:
            raise KakiError("回答不能为空或过长")
        value = {"text": text}
        if q["kind"] == "approval":
            allowed = q["operation"].get("grant_scopes", rt.config.permissions.grant_scopes) + ["deny"]
            selected = choice or next(
                (key for key, label in APPROVAL_LABELS.items() if text.strip() in (key, label)), None
            )
            if selected not in allowed or text.strip() not in (selected, APPROVAL_LABELS[selected]):
                raise KakiError("请使用明确的授权选项；附加限制请拒绝后重新提交任务")
            value["choice"] = selected
        response = await rt.store.run("decision_answer", identity, rt.session_id, rt.active_turn, value)
        if q["future"].done():
            raise KakiError("回答提交期间任务已停止或问题已经回答")
        self.answered[identity] = {
            "turn_id": rt.active_turn,
            "session_id": rt.session_id,
            "text": text,
            "choice": choice,
        }
        q["future"].set_result({**response, "decision_id": identity})
        await rt.emit("question.answered", rt.active_turn, {"question_id": identity, "kind": q["kind"]})
