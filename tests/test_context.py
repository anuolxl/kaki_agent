import asyncio

import pytest
from conftest import ScriptedProvider

from kaki.context import ContextEngine
from kaki.context.curator import parse_plan
from kaki.context.history import canonical, page_tool_exchanges, shrink_tool_text, structural_errors
from kaki.context.knowledge import Skills, rank
from kaki.context.tokens import estimate
from kaki.store import Store
from kaki.types import ContextOverflow, KakiError, Message, ModelResponse, ToolCall, TurnResult


def test_pair_closure_and_shrink_are_lossless_to_source():
    history = [
        Message("user", "task", id=1),
        Message("assistant", tool_calls=[{"id": "a"}, {"id": "b"}], id=2),
        Message("tool", "A" * 500, id=3, tool_call_id="a"),
        Message("tool", "B" * 500, id=4, tool_call_id="b"),
        Message("assistant", "done", id=5),
    ]
    closed = canonical(history, [1, 3])
    assert [m.id for m in closed] == [1, 2, 3, 4]
    assert not structural_errors(closed)
    assert structural_errors([history[0], history[2]])
    shrunk, changed = shrink_tool_text(history, 1)
    assert changed == 1 and shrunk[2].content != history[2].content
    assert history[2].content == "A" * 500


def test_native_budget_paging_keeps_parallel_calls_with_their_reasoning():
    history = [Message("user", "initial immutable constraint", id=1)]
    for i in range(5):
        native = [{"type": "reasoning", "id": f"r{i}", "encrypted_content": "opaque-" * 1000}]
        calls = []
        for suffix in ["a", "b"]:
            calls.append({"id": f"{i}{suffix}", "name": "read", "arguments": {}})
            native.append(
                {"type": "function_call", "call_id": f"{i}{suffix}", "name": "read", "arguments": "{}"}
            )
        history.append(Message("assistant", id=2 + 3 * i, tool_calls=calls, native_items=native))
        history.extend(
            Message("tool", "observation", id=3 + 3 * i + j, tool_call_id=f"{i}{suffix}")
            for j, suffix in enumerate(["a", "b"])
        )
    limit = estimate([history[0], *history[-3:]], []) + 20
    selected, removed = page_tool_exchanges(history, [], limit)
    assert selected == [history[0], *history[-3:]]
    assert len(removed) == 12
    assert not structural_errors(selected)
    assert selected[1].native_items == history[-3].native_items


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["slow", "invalid", "timeout"])
async def test_curator_fallback_budget_archive_and_raw_history(workspace, config, mode):
    config.agents.defaults.context_window_tokens = 8192
    config.agents.defaults.max_tokens = 512
    config.context.protect_first_n = 1
    config.context.curator_timeout_seconds = 0.05
    model = ScriptedProvider(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    "plan",
                    "kaki_context_build",
                    {"include_message_ids": [1, 2], "working_state_injection": "保留最初约束"},
                )
            ]
        )
    )
    if mode == "invalid":
        model = ScriptedProvider(ModelResponse(text="not a plan"))
    elif mode == "timeout":
        model.block = asyncio.Event()
    store = Store(workspace.state / "test.sqlite3", workspace.id)
    await store.open()
    try:
        session = await store.run("new_session")
        for i in range(14):
            t, _, _ = await store.run("accept_turn", session, f"第{i}个任务 上海路线" + " old detail" * 80)
            await store.run("commit_model", session, t, ModelResponse(text=("answer old detail " * 250)))
            await store.run("finish_turn", TurnResult(t, "completed", "ok", "final"))
        history = await store.run("messages", session)
        t, user, _ = await store.run("accept_turn", session, "继续上海路线")
        e = ContextEngine(workspace, store, config, model)
        window = await e.assemble(session, history, user, [], force=True)
        assert window.metadata["path"] == ("slow" if mode == "slow" else "fallback")
        assert estimate(window.messages, []) <= window.budget.input_limit
        assert not structural_errors(window.messages)
        assert {1, 2} <= set(window.metadata["included_ids"])
        assert len(await store.run("messages", session)) == len(history) + 1
        assert window.metadata.get("archive_ref")
        payload, _ = await e.archive.load(session, window.metadata["archive_ref"])
        omitted = {m.id: m for m in history if m.id not in window.metadata["included_ids"]}
        assert {m["id"]: m["content"] for m in payload["messages"]} == {
            i: m.content for i, m in omitted.items()
        }
        second = await store.run("new_session")
        with pytest.raises(KakiError):
            await e.archive.load(second, window.metadata["archive_ref"])
        state = await store.run("context_get", session)
        assert state["manifest"]["1"]["relevance"] > 0.5 * 0.95
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fixed_input_cannot_be_silently_dropped(workspace, config):
    config.agents.defaults.context_window_tokens = 2048
    config.agents.defaults.max_tokens = 512
    s = Store(workspace.state / "test.sqlite3", workspace.id)
    await s.open()
    try:
        session = await s.run("new_session")
        t, user, _ = await s.run("accept_turn", session, "超长用户原始约束" * 3000)
        e = ContextEngine(workspace, s, config, ScriptedProvider())
        with pytest.raises(ContextOverflow):
            await e.assemble(session, [], user, [])
        assert len(await s.run("messages", session)) == 1
    finally:
        await s.close()


@pytest.mark.asyncio
async def test_active_turn_rebalance_and_archive(workspace, config):
    config.agents.defaults.context_window_tokens = 6000
    config.agents.defaults.max_tokens = 512
    config.context.protect_first_n = 0
    s = Store(workspace.state / "test.sqlite3", workspace.id)
    await s.open()
    try:
        from kaki.types import ToolResult

        session = await s.run("new_session")
        turn, user, _ = await s.run("accept_turn", session, "run a large task")
        e = ContextEngine(workspace, s, config, ScriptedProvider())
        window = await e.assemble(session, [], user, [])
        for i in range(5):
            m, runs = await s.run(
                "commit_model", session, turn, ModelResponse(tool_calls=[ToolCall(str(i), "read", {})])
            )
            await s.run("mark_running", runs[0]["id"])
            tool = await s.run(
                "commit_tool", session, turn, runs[0]["id"], ToolResult("large log content " * 500)
            )
            window.messages.extend([m, tool])
        updated = await e.rebalance(session, window, [])
        assert updated.estimated_tokens <= updated.budget.input_limit
        assert not structural_errors(updated.messages)
        assert len(await s.run("messages", session)) == 11
    finally:
        await s.close()


def test_local_skills_precedence_references_and_cjk(workspace, config, tmp_path):
    root = workspace.root / ".kaki/skills/shanghai"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: shanghai\ndescription: 上海 秋天 旅游 线路\n---\n先核对官方来源。参考[详情](details.md)\n"
    )
    (root / "details.md").write_text("真实来源才可以引用")
    skills = Skills(workspace, config)
    text, metadata = skills.resolve("$shanghai 上海秋天旅游")
    assert metadata["injected_skill_ids"] == ["shanghai"]
    assert "先核对官方来源" in text
    assert skills.read(str(root / "details.md")) == "真实来源才可以引用"
    with pytest.raises(KakiError):
        skills.read(str(tmp_path / "outside"))
    assert rank("上海秋天", ["北京冬季", "上海秋天散步"])[0][0] == 1


def test_invalid_plan_types():
    with pytest.raises(KakiError):
        parse_plan({"include_message_ids": [True]})
    with pytest.raises(KakiError):
        parse_plan({"made_up": []})


@pytest.mark.asyncio
@pytest.mark.parametrize("planner", ["valid", "invalid", "timeout"])
async def test_resume_large_protected_tool_turn_pages_observations_without_losing_facts(
    workspace, config, planner
):
    from kaki.types import ToolResult

    config.agents.defaults.context_window_tokens = 8192
    config.agents.defaults.max_tokens = 512
    config.context.protect_first_n = 3
    config.context.curator_timeout_seconds = 0.05
    provider = ScriptedProvider(ModelResponse(text="not a plan"))
    store = Store(workspace.state / "test.sqlite3", workspace.id)
    await store.open()
    try:
        session = await store.run("new_session")
        turn, original, _ = await store.run(
            "accept_turn", session, "Only edit source.go; preserve the user's constraints"
        )
        for i in range(20):
            _, runs = await store.run(
                "commit_model",
                session,
                turn,
                ModelResponse(
                    tool_calls=[ToolCall(f"read-{i}", "read", {"path": "source.go"})],
                    native_items=[
                        {"type": "reasoning", "id": f"reason-{i}", "encrypted_content": "opaque-" * 2000},
                        {
                            "type": "function_call",
                            "call_id": f"read-{i}",
                            "name": "read",
                            "arguments": '{"path":"source.go"}',
                        },
                    ],
                ),
            )
            await store.run("mark_running", runs[0]["id"])
            await store.run(
                "commit_tool", session, turn, runs[0]["id"], ToolResult("large source evidence " * 400)
            )
        await store.run("finish_turn", TurnResult(turn, "interrupted", "budget reached", "iteration_limit"))
        history = await store.run("messages", session)
        if planner == "valid":
            provider = ScriptedProvider(
                ModelResponse(
                    tool_calls=[
                        ToolCall(
                            "plan", "kaki_context_build", {"include_message_ids": [m.id for m in history]}
                        )
                    ]
                )
            )
        elif planner == "timeout":
            provider.block = asyncio.Event()
        _, user, _ = await store.run(
            "accept_turn", session, "Continue the same task without repeating completed work"
        )
        engine = ContextEngine(workspace, store, config, provider)
        window = await engine.assemble(session, history, user, [], force=True)
        assert window.estimated_tokens <= window.budget.input_limit
        assert not structural_errors(window.messages)
        assert any(m.id == original.id and m.content == original.content for m in window.messages)
        assert window.metadata["paged_exchange_ids"]
        assert [m for m in window.messages if m.role == "tool"][-1].content == [
            m for m in history if m.role == "tool"
        ][-1].content
        payload, _ = await engine.archive.load(session, window.metadata["archive_ref"])
        archived = {m["id"]: m["content"] for m in payload["messages"]}
        for m in history:
            if m.id in window.metadata["elided_tool_ids"] + window.metadata["paged_exchange_ids"]:
                assert archived[m.id] == m.content
        last_assistant = [m for m in window.messages if m.role == "assistant"][-1]
        assert last_assistant.native_items == [m for m in history if m.role == "assistant"][-1].native_items
        persisted = await store.run("messages", session)
        assert [(m.id, m.content) for m in persisted[:-1]] == [(m.id, m.content) for m in history]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_explicit_memory_versions_and_no_secrets(workspace, config):
    from kaki.context.knowledge import Memory

    s = Store(workspace.state / "test.sqlite3", workspace.id)
    await s.open()
    try:
        session = await s.run("new_session")
        t, u, _ = await s.run("accept_turn", session, "记住我喜欢上海秋天")
        memory = Memory(s, config)
        old = await memory.remember("喜欢上海秋天散步", [u.id])
        new = await memory.remember("喜欢上海秋天骑车", [u.id], replaces=old)
        rows = await memory.recall("上海秋天")
        assert [r["id"] for r in rows] == [new]
        assert rows[0]["version"] == 2
        with pytest.raises(KakiError):
            await memory.remember("sk-abcdefghijklmnopqrstuv", [u.id])
        await s.run("retract", config.memory.user_id, new)
        assert await memory.recall("上海秋天") == []
    finally:
        await s.close()
