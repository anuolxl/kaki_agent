import asyncio
import json

import pytest
from conftest import ScriptedProvider

from kaki.context.history import structural_errors
from kaki.runtime import Runtime
from kaki.store import Store
from kaki.types import KakiError, ModelResponse, ToolCall, ToolResult, TurnResult


@pytest.mark.asyncio
async def test_crash_closes_pairs_without_replaying(workspace):
    store = Store(workspace.state / "test.sqlite3", workspace.id)
    await store.open()
    session = await store.run("new_session")
    turn, _, _ = await store.run("accept_turn", session, "change file", "request-1")
    _, runs = await store.run(
        "commit_model",
        session,
        turn,
        ModelResponse(
            tool_calls=[
                ToolCall("a", "kaki_write_file", {}),
                ToolCall("b", "kaki_exec", {}),
                ToolCall("c", "kaki_read_file", {}),
            ]
        ),
    )
    await store.run("mark_running", runs[0]["id"])
    await store.run("commit_tool", session, turn, runs[0]["id"], ToolResult("saved"))
    await store.run("mark_running", runs[1]["id"])
    await store.close()
    store = Store(workspace.state / "test.sqlite3", workspace.id)
    await store.open()
    try:
        await store.run("recover", session)
        states = [r["state"] for r in await store.run("tool_runs", turn)]
        assert states == ["succeeded", "unknown", "cancelled"]
        assert not structural_errors(await store.run("messages", session))
        rows = await store.run("turns", session)
        assert rows[0]["status"] == "interrupted"
        assert await store.run("recover", session) == []
        assert not (workspace.root / "changed").exists()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_real_file_and_shell_loop_and_idempotency(workspace, config):
    (workspace.root / "maths.py").write_text("def double(x):\n    return x + x + 1\n")
    model = ScriptedProvider(
        ModelResponse(tool_calls=[ToolCall("r", "kaki_read_file", {"path": "maths.py"})]),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    "e", "kaki_edit_file", {"path": "maths.py", "old_text": "x + x + 1", "new_text": "x + x"}
                )
            ]
        ),
        ModelResponse(tool_calls=[ToolCall("t", "kaki_exec", {"command": "python3 -m unittest -v"})]),
        ModelResponse(text="已修复，测试通过。"),
    )
    (workspace.root / "test_maths.py").write_text(
        "import unittest\nfrom maths import double\nclass Test(unittest.TestCase):\n    def test_double(self):\n        self.assertEqual(double(2), 4)\n"
    )
    rt = Runtime(workspace, config, provider=model)
    events = []

    async def on_event(e):
        events.append(e)

    rt.listeners.append(on_event)
    try:
        await rt.initialize()
        result = await rt.run("修复double并运行测试", "same-request")
        assert result.status == "completed", result
        assert result.verification["status"] == "passed"
        assert result.tools == 3
        assert "x + x + 1" not in (workspace.root / "maths.py").read_text()
        assert len([e for e in events if e.type == "turn.finished"]) == 1
        duplicate = await rt.run("修复double并运行测试", "same-request")
        assert duplicate.turn_id == result.turn_id
        assert len(model.calls) == 4
        assert len(await rt.store.run("turns", rt.session_id)) == 1
        assert not structural_errors(await rt.store.run("messages", rt.session_id))
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_cancel_stream_resume_and_continue(workspace, config):
    model = ScriptedProvider(ModelResponse(text="不会到达"))
    model.block = asyncio.Event()
    rt = Runtime(workspace, config, provider=model)
    await rt.initialize()
    session = rt.session_id
    task = rt.submit("等待")
    await asyncio.wait_for(model.entered.wait(), 5)
    await rt.cancel()
    assert (await task).status == "cancelled"
    assert (await rt.store.run("turns", session))[0]["status"] == "cancelled"
    await rt.close()
    rt = Runtime(workspace, config, provider=ScriptedProvider(ModelResponse(text="恢复后正常")))
    try:
        await rt.initialize(session)
        assert rt.recovery_plan["items"]
        result = await rt.run("继续")
        assert result.status == "completed"
        assert "unknown" not in result.text
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_ask_user_answer_and_iteration_limit(workspace, config):
    config.agents.defaults.max_tool_iterations = 1
    model = ScriptedProvider(
        ModelResponse(
            tool_calls=[ToolCall("q", "kaki_ask_user", {"question": "选择颜色", "options": ["蓝", "绿"]})]
        ),
        ModelResponse(text="已收到蓝色，达到预算。"),
    )
    rt = Runtime(workspace, config, provider=model)

    async def answer(e):
        if e.type == "question.requested":
            await rt.answer(e.data["question_id"], "蓝")

    rt.listeners.append(answer)
    try:
        await rt.initialize()
        r = await rt.run("询问颜色")
        assert r.status == "interrupted" and r.stop_reason == "iteration_limit"
        assert model.calls[-1][1] is None
        assert "蓝" in (await rt.store.run("messages", rt.session_id))[-2].content
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_terminal_immutable_and_request_mismatch(workspace):
    s = Store(workspace.state / "test.sqlite3", workspace.id)
    await s.open()
    try:
        session = await s.run("new_session")
        turn, _, _ = await s.run("accept_turn", session, "one", "key")
        await s.run("finish_turn", TurnResult(turn, "completed", "done", "final"))
        with pytest.raises(KakiError):
            await s.run("finish_turn", TurnResult(turn, "failed", "bad", "second"))
        with pytest.raises(KakiError):
            await s.run("accept_turn", session, "different", "key")
        assert json.loads((await s.run("turns", session))[0]["result_json"])["status"] == "completed"
    finally:
        await s.close()
