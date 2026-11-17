import asyncio
import sqlite3

import pytest
from conftest import ScriptedProvider

from kaki.app_controller import AppController, render_markdown
from kaki.runtime import Runtime
from kaki.store import Store
from kaki.types import KakiError, ModelResponse, ToolCall, TurnResult


def controller(config, provider):
    return AppController(config, runtime_factory=lambda ws, cfg: Runtime(ws, cfg, provider=provider))


@pytest.mark.asyncio
async def test_app_session_management_and_persistence(workspace, config):
    app = controller(config, ScriptedProvider(ModelResponse(text="hello")))
    try:
        engine = await app.open_workspace(str(workspace.root))
        wid, sid = workspace.id, engine.session
        await app.send(wid, sid, "say hello")
        await engine.task
        assert engine.phase == "completed" and not engine.busy
        detail = await app.detail(wid, sid)
        assert detail["messages"][-1]["text"] == "hello"
        assert detail["turns"][-1]["status"] == "completed"
        await app.rename(wid, sid, " renamed session ")
        assert (await app.sessions(wid))[0]["title"] == "renamed session"
        await app.archive(wid, sid, True)
        assert await app.sessions(wid) == []
        assert (await app.sessions(wid, True))[0]["id"] == sid
        with pytest.raises(KakiError):
            await app.send(wid, sid, "archived")
        await app.archive(wid, sid, False)
    finally:
        await app.close()
    app = controller(config, ScriptedProvider())
    try:
        e = await app.open_workspace(str(workspace.root))
        assert e.session == sid
        assert (await app.detail(wid, sid))["messages"][-1]["text"] == "hello"
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_committed_text_is_not_a_finished_turn(workspace, config):
    config.runtime.checkpoint.policy = "always"
    app = controller(config, ScriptedProvider(ModelResponse(text="answer already visible")))
    after_entered, after_release = asyncio.Event(), asyncio.Event()

    async def snapshot(turn, phase):
        if phase == "after":
            after_entered.set()
            await after_release.wait()
        return {"id": "snapshot-" + phase, "state": "ready"}

    try:
        e = await app.open_workspace(str(workspace.root))
        e.runtime.checkpoint.capture = snapshot
        await app.send(workspace.id, e.session, "answer")
        await asyncio.wait_for(after_entered.wait(), 5)
        detail = await app.detail(workspace.id, e.session)
        assert detail["messages"][-1]["text"] == "answer already visible"
        assert detail["turns"][-1]["status"] == "running"
        assert detail["live"]["busy"] and detail["live"]["phase"] == "finishing"
        with pytest.raises(KakiError):
            await app.send(workspace.id, e.session, "too early")
        after_release.set()
        await e.task
        assert not e.busy and e.phase == "completed"
    finally:
        after_release.set()
        await app.close()


@pytest.mark.asyncio
async def test_stop_keeps_app_and_other_session_readable(workspace, config):
    provider = ScriptedProvider(ModelResponse(text="retry succeeded"))
    provider.block = asyncio.Event()
    app = controller(config, provider)
    try:
        e = await app.open_workspace(str(workspace.root))
        sid = e.session
        await app.send(workspace.id, sid, "wait")
        await asyncio.wait_for(provider.entered.wait(), 5)
        other = await app.new_session(workspace.id)
        assert (await app.detail(workspace.id, other))["live"]["session_id"] == sid
        with pytest.raises(KakiError):
            await app.send(workspace.id, other, "other write")
        with pytest.raises(KakiError):
            await app.archive(workspace.id, sid, True)
        await app.stop(workspace.id, sid)
        assert e.phase == "cancelled" and not app.closed
        assert (await app.detail(workspace.id, sid))["turns"][-1]["status"] == "cancelled"
        provider.block = None
        await app.send(workspace.id, other, "try again")
        await e.task
        assert (await app.detail(workspace.id, other))["messages"][-1]["text"] == "retry succeeded"
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_question_is_scoped_to_active_session(workspace, config):
    p = ScriptedProvider(
        ModelResponse(tool_calls=[ToolCall("q", "kaki_ask_user", {"question": "颜色？", "options": ["绿"]})]),
        ModelResponse(text="green"),
    )
    app = controller(config, p)
    requested = asyncio.Event()
    try:
        e = await app.open_workspace(str(workspace.root))
        sid = e.session

        async def observe(event):
            if event.type == "question.requested":
                requested.set()

        e.runtime.listeners.append(observe)
        await app.send(workspace.id, sid, "ask")
        await asyncio.wait_for(requested.wait(), 5)
        assert e.phase == "waiting_user"
        other = await app.new_session(workspace.id)
        with pytest.raises(KakiError):
            await app.answer(workspace.id, other, e.runtime.question["id"], "绿")
        await app.answer(workspace.id, sid, e.runtime.question["id"], "绿")
        await e.task
        assert e.phase == "completed"
        detail = await app.detail(workspace.id, sid)
        assert detail["turns"][-1]["tools"][0]["tool_call_id"] == "q"
        assert detail["live"]["tools"][0]["arguments"]["question"] == "颜色？"
    finally:
        await app.close()


@pytest.mark.asyncio
async def test_shutdown_skips_new_after_snapshot_but_persists_cancel(workspace, config):
    config.runtime.checkpoint.policy = "always"
    p = ScriptedProvider(ModelResponse(text="never"))
    p.block = asyncio.Event()
    app = controller(config, p)
    phases = []

    async def capture(turn, phase):
        phases.append(phase)
        return {"id": "snap", "state": "ready"}

    e = await app.open_workspace(str(workspace.root))
    sid = e.session
    e.runtime.checkpoint.capture = capture
    await app.send(workspace.id, sid, "wait")
    await asyncio.wait_for(p.entered.wait(), 5)
    await asyncio.wait_for(app.close(), 5)
    assert phases == ["before"]
    store = Store(workspace.state / "kaki.sqlite3", workspace.id)
    await store.open()
    try:
        assert (await store.run("turns", sid))[-1]["status"] == "cancelled"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_v1_database_migration_preserves_facts_and_backup(workspace):
    path = workspace.state / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE sessions(id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,title TEXT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL);PRAGMA user_version=1;"
        )
        db.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?)", ("old", workspace.id, "历史对话", "2020", "2020")
        )
    store = Store(path, workspace.id)
    await store.open()
    try:
        rows = await store.run("sessions")
        assert rows[0]["title"] == "历史对话" and rows[0]["archived"] == 0
        assert list(workspace.state.glob("old.sqlite3.v1-*.backup"))
        await store.run("new_session")
        await store.run("archive_session", "old", True)
        assert len(await store.run("sessions")) == 1
    finally:
        await store.close()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    backup = next(workspace.state.glob("old.sqlite3.v1-*.backup"))
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT title FROM sessions").fetchone()[0] == "历史对话"
    assert backup.stat().st_mode & 0o777 == 0o600


def test_rendering_does_not_execute_model_html_or_load_images():
    output = render_markdown(
        "<script>alert(1)</script>\n![track](https://example.com/pixel)\n[bad](javascript:alert(1))"
    )
    assert "<script>" not in output and "<img " not in output and 'href="javascript:' not in output


@pytest.mark.asyncio
async def test_migration_preserves_messages_results_and_live_wal(workspace):
    path = workspace.state / "history.sqlite3"
    store = Store(path, workspace.id)
    await store.open()
    sid = await store.run("new_session", "original")
    turn, user, _ = await store.run("accept_turn", sid, "persist my question", "request-before-upgrade")
    await store.run("commit_model", sid, turn, ModelResponse(text="persist my answer"))
    await store.run("finish_turn", TurnResult(turn, "completed", "persist my answer", "final_answer"))
    await store.close()
    # Emulate the previous schema and leave the title update in WAL during the upgrade.
    with sqlite3.connect(path) as previous:
        previous.execute("ALTER TABLE sessions DROP COLUMN archived")
        previous.execute("PRAGMA user_version=1")
        previous.execute("UPDATE sessions SET title='title in WAL'")
        previous.commit()
        assert path.with_name(path.name + "-wal").exists()
        upgraded = Store(path, workspace.id)
        await upgraded.open()
        try:
            messages = await upgraded.run("messages", sid)
            assert [m.content for m in messages] == ["persist my question", "persist my answer"]
            assert messages[0].id == user.id
            assert (await upgraded.run("turns", sid))[0]["status"] == "completed"
            assert (await upgraded.run("sessions"))[0]["title"] == "title in WAL"
        finally:
            await upgraded.close()
        backup = next(workspace.state.glob("history.sqlite3.v1-*.backup"))
        with sqlite3.connect(backup) as saved:
            assert saved.execute("SELECT title FROM sessions").fetchone()[0] == "title in WAL"
            assert saved.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
            assert saved.execute("PRAGMA user_version").fetchone()[0] == 1
