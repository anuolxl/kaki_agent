import asyncio
import json
import sqlite3

import pytest
from conftest import ScriptedProvider

from kaki.context.history import structural_errors
from kaki.decisions import APPROVAL_LABELS
from kaki.permissions import structured_command
from kaki.runtime import Runtime
from kaki.store import Store
from kaki.types import KakiError, Message, ModelResponse, ToolCall
from kaki.understanding import UnderstandingService
from kaki.understanding.retrieval import fused_rank
from kaki.understanding.strategies import local_analysis


def analysis_response(
    original="解释超时", *, source_ids=None, standalone=None, name="kaki_understanding_build"
):
    return ModelResponse(
        tool_calls=[
            ToolCall(
                "private",
                name,
                {
                    "intent": {"domain": "coding", "actions": ["explain"]},
                    "rewrite": {
                        "standalone_query": standalone or original,
                        "queries": {"code": ["timeout"], "history": ["timeout"]},
                        "source_ids": source_ids or [],
                        "unresolved": [],
                    },
                },
            )
        ]
    )


@pytest.mark.parametrize("intent_strategy", ["rules", "llm", "hybrid"])
@pytest.mark.parametrize("rewrite_strategy", ["local", "llm", "hybrid"])
async def test_independent_strategies_share_one_attempt(config, intent_strategy, rewrite_strategy):
    config.understanding.enabled = True
    config.understanding.intent.strategy = intent_strategy
    config.understanding.rewrite.strategy = rewrite_strategy
    model = ScriptedProvider(analysis_response())
    service = UnderstandingService(config, model)
    user = Message("user", "解释超时", id=1, turn_id="turn")
    result = await service.prepare(user, [])
    expected = intent_strategy == "llm" or rewrite_strategy == "llm"
    assert len(model.calls) == int(expected)
    if expected:
        assert model.calls[0][2]["max_attempts"] == 1
        assert model.calls[0][2]["kind"] == "understanding"
    assert result.original_text == user.content
    assert result.strategy["intent"] == ("llm" if intent_strategy == "llm" else "rules")
    assert result.strategy["rewrite"] == ("llm" if rewrite_strategy == "llm" else "local")
    assert result.queries_for("code")[0] == user.content


async def test_timeout_falls_back_without_second_attempt_and_cancel_propagates(config):
    config.understanding.enabled = True
    config.understanding.intent.strategy = "llm"
    config.understanding.llm.timeout_seconds = 0.02
    model = ScriptedProvider(analysis_response())
    model.block = asyncio.Event()
    service = UnderstandingService(config, model)
    result = await service.prepare(Message("user", "解释超时"), [])
    assert result.fallback == "timeout" and len(model.calls) == 1
    assert result.strategy["intent"] == "rules"
    config.understanding.llm.timeout_seconds = 10
    model.entered.clear()
    task = asyncio.create_task(service.prepare(Message("user", "解释超时"), []))
    await asyncio.wait_for(model.entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_invalid_evidence_only_falls_back_rewrite(config):
    config.understanding.enabled = True
    config.understanding.intent.strategy = "llm"
    config.understanding.rewrite.strategy = "llm"
    model = ScriptedProvider(analysis_response(source_ids=[999]))
    result = await UnderstandingService(config, model).prepare(Message("user", "解释超时"), [])
    assert result.strategy["intent"] == "llm"
    assert result.strategy["rewrite"] == "local"
    assert result.fallback == "rewrite_invalid"
    assert len(model.calls) == 1


@pytest.mark.parametrize(
    "original,rewritten",
    [("只回答，不修改文件", "修改文件"), ("解释MyConfig.py的10s超时", "解释myconfig.py的1s超时")],
)
async def test_model_cannot_drop_constraints_identifiers_numbers(config, original, rewritten):
    config.understanding.enabled = True
    config.understanding.rewrite.strategy = "llm"
    result = await UnderstandingService(
        config, ScriptedProvider(analysis_response(original, standalone=rewritten))
    ).prepare(Message("user", original), [])
    assert result.fallback == "rewrite_invalid"
    assert result.rewrite.standalone_query == original


def test_local_test_plan_not_execution_and_fusion_preserves_original(config):
    intent, plan = local_analysis("设计测试方案，解释会话超时", [], config.understanding)
    assert "plan_tests" in intent.actions and "run_tests" not in intent.actions
    assert plan.queries["code"][0] == "设计测试方案，解释会话超时"
    assert "timeout" in plan.queries["code"][1]
    assert [i for i, _ in fused_rank(["origin", "timeout"], ["origin", "timeout", "unrelated"])] == [0, 1]


@pytest.mark.parametrize(
    "command",
    [
        "git push origin master",
        "python3 -c 'print(1)'",
        "echo x > file",
        "echo $(pwd)",
        "cat .env",
        "node script.js",
        "npm install",
        "sudo pwd",
        "pytest ../test.py",
    ],
)
def test_shell_rejects_unsupported_grammar_and_capabilities(command):
    with pytest.raises(KakiError):
        structured_command(command)


async def test_denied_write_is_not_started_paired_or_snapshotted(workspace, config):
    config.runtime.checkpoint.policy = "always"
    provider = ScriptedProvider(
        ModelResponse(tool_calls=[ToolCall("w", "kaki_write_file", {"path": "bad.txt", "content": "bad"})]),
        ModelResponse(text="未修改文件"),
    )
    rt = Runtime(workspace, config, provider=provider)
    try:
        await rt.initialize()
        result = await rt.run("只回答，不修改文件")
        runs = await rt.store.run("tool_runs", result.turn_id)
        assert runs[0]["state"] == "denied" and runs[0]["started"] is None
        assert not json.loads(runs[0]["result_json"])["executed"]
        assert result.before_snapshot is None and result.after_snapshot is None
        assert not (workspace.root / "bad.txt").exists()
        assert not structural_errors(await rt.store.run("messages", rt.session_id))
        assert result.verification["status"] == "not_run"
    finally:
        await rt.close()


async def test_human_approval_invalid_reply_retry_and_turn_grant(workspace, config):
    p = ScriptedProvider(
        ModelResponse(tool_calls=[ToolCall("w1", "kaki_write_file", {"path": "new.txt", "content": "one"})]),
        ModelResponse(tool_calls=[ToolCall("w2", "kaki_write_file", {"path": "new.txt", "content": "two"})]),
        ModelResponse(text="done"),
    )
    rt = Runtime(workspace, config, provider=p)
    questions = []

    async def answer(event):
        if event.type != "question.requested":
            return
        questions.append(event)
        assert event.data["kind"] == "approval"
        qid = event.data["question_id"]
        with pytest.raises(KakiError):
            await rt.answer(qid, "可以，但别改tests")
        assert rt.question and not rt.question["future"].done()
        await rt.answer(qid, APPROVAL_LABELS["turn"], "turn")
        await rt.answer(qid, APPROVAL_LABELS["turn"], "turn")

    rt.listeners.append(answer)
    try:
        await rt.initialize()
        result = await rt.run("看看当前仓库")
        assert result.status == "completed", result
        assert len(questions) == 1 and (workspace.root / "new.txt").read_text() == "two"
        decisions = await rt.store.run("decisions", result.turn_id)
        assert decisions[0]["state"] == "answered"
        with pytest.raises(KakiError):
            await rt.answer(questions[0].data["question_id"], "turn")
    finally:
        await rt.close()


async def test_clarification_same_turn_raw_input_preserved_and_single_model_budget(workspace, config):
    config.understanding.enabled = True
    config.understanding.intent.strategy = "llm"
    config.understanding.rewrite.strategy = "llm"
    response = analysis_response("修改它")
    response.tool_calls[0].arguments["rewrite"]["unresolved"] = ["请提供文件路径"]
    p = ScriptedProvider(response, ModelResponse(text="明确目标为a.py"))
    rt = Runtime(workspace, config, provider=p)

    async def answer(event):
        if event.type == "question.requested":
            assert event.data["kind"] == "clarification"
            await rt.answer(event.data["question_id"], "a.py中的double函数")

    rt.listeners.append(answer)
    try:
        await rt.initialize()
        result = await rt.run("修改它")
        assert result.status == "completed", result
        messages = await rt.store.run("messages", rt.session_id)
        assert [m.content for m in messages if m.role == "user"] == ["修改它", "a.py中的double函数"]
        assert len({m.turn_id for m in messages}) == 1
        assert [c[2]["kind"] for c in p.calls] == ["understanding", "main"]
        assert p.calls[-1][0][-1].content == "修改它"
    finally:
        await rt.close()


async def test_noninteractive_approval_interrupts_without_hanging(workspace, config):
    p = ScriptedProvider(
        ModelResponse(tool_calls=[ToolCall("w", "kaki_write_file", {"path": "x.txt", "content": "x"})])
    )
    rt = Runtime(workspace, config, provider=p, interactive=False)
    try:
        await rt.initialize()
        result = await asyncio.wait_for(rt.run("看看仓库"), 3)
        assert result.status == "interrupted" and result.stop_reason == "approval_required"
        assert not (workspace.root / "x.txt").exists()
    finally:
        await rt.close()


async def test_denied_test_is_not_verification_and_memory_not_inferred(workspace, config):
    config.permissions.mode = "read_only"
    p = ScriptedProvider(
        ModelResponse(
            tool_calls=[
                ToolCall("t", "kaki_exec", {"command": "python3 -m unittest"}),
                ToolCall("m", "kaki_remember", {"content": "guess"}),
            ]
        ),
        ModelResponse(text="未执行"),
    )
    rt = Runtime(workspace, config, provider=p)
    try:
        await rt.initialize()
        result = await rt.run("设计测试方案")
        assert result.verification == {"status": "not_run", "commands": []}
        assert [r["state"] for r in await rt.store.run("tool_runs", result.turn_id)] == ["denied", "denied"]
        assert await rt.store.run("memories", config.memory.user_id) == []
    finally:
        await rt.close()


async def test_v2_backup_and_pending_decision_expired_on_recovery(workspace):
    path = workspace.state / "migration.sqlite3"
    store = Store(path, workspace.id)
    await store.open()
    session = await store.run("new_session")
    turn, _, _ = await store.run("accept_turn", session, "pending", None)
    await store.run("decision_create", "q", session, turn, "approval", "allow?", [], {})
    await store.close()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=2")
    store = Store(path, workspace.id)
    await store.open()
    try:
        assert list(path.parent.glob(path.name + ".v2-*.backup"))
        await store.run("recover", session)
        assert (await store.run("decisions", turn))[0]["state"] == "expired"
        assert (await store.run("turns", session))[0]["status"] == "interrupted"
    finally:
        await store.close()


async def test_model_decide_ambiguity_skips_preflight_but_not_permissions(workspace, config):
    config.understanding.enabled = True
    config.understanding.intent.strategy = "rules"
    config.understanding.rewrite.strategy = "local"
    config.understanding.ambiguity.policy = "model_decide"
    p = ScriptedProvider(ModelResponse(text="请提供文件路径"))
    rt = Runtime(workspace, config, provider=p)
    try:
        await rt.initialize()
        result = await rt.run("修改它")
        assert result.status == "completed"
        assert not await rt.store.run("decisions", result.turn_id)
        assert len(p.calls) == 1 and "unresolved" in p.calls[0][0][0].content
    finally:
        await rt.close()


async def test_model_intent_does_not_grant_write_permission(workspace, config):
    config.understanding.enabled = True
    config.understanding.intent.strategy = "llm"
    config.permissions.default_decision = "deny"
    analysis = analysis_response("看看项目")
    analysis.tool_calls[0].arguments["intent"]["actions"] = ["edit"]
    p = ScriptedProvider(
        analysis,
        ModelResponse(
            tool_calls=[ToolCall("w", "kaki_write_file", {"path": "x.txt", "content": "not authorized"})]
        ),
        ModelResponse(text="未修改"),
    )
    rt = Runtime(workspace, config, provider=p)
    try:
        await rt.initialize()
        result = await rt.run("看看项目")
        assert (await rt.store.run("tool_runs", result.turn_id))[0]["state"] == "denied"
        assert not (workspace.root / "x.txt").exists()
    finally:
        await rt.close()


async def test_cancel_pending_approval_expires_decision_and_does_not_leak_grant(workspace, config):
    p = ScriptedProvider(
        ModelResponse(tool_calls=[ToolCall("w", "kaki_write_file", {"path": "x.txt", "content": "x"})])
    )
    rt = Runtime(workspace, config, provider=p)
    pending = asyncio.Event()
    rt.listeners.append(lambda event: signal_question(event, pending))
    try:
        await rt.initialize()
        task = rt.submit("看看项目")
        await asyncio.wait_for(pending.wait(), 3)
        qid = rt.question["id"]
        await rt.cancel()
        result = await task
        assert result.status == "cancelled"
        assert (await rt.store.run("decisions", result.turn_id))[0]["state"] == "expired"
        assert not rt.permissions.grants and not (workspace.root / "x.txt").exists()
        with pytest.raises(KakiError):
            await rt.answer(qid, "once")
    finally:
        await rt.close()


async def signal_question(event, signal):
    if event.type == "question.requested":
        signal.set()


async def test_file_changed_during_approval_is_not_overwritten(workspace, config):
    path = workspace.root / "x.txt"
    path.write_text("before")
    p = ScriptedProvider(
        ModelResponse(tool_calls=[ToolCall("w", "kaki_write_file", {"path": "x.txt", "content": "agent"})]),
        ModelResponse(text="文件已改变，未覆盖"),
    )
    rt = Runtime(workspace, config, provider=p)

    async def answer(event):
        if event.type == "question.requested":
            path.write_text("user concurrent edit")
            await rt.answer(event.data["question_id"], "once")

    rt.listeners.append(answer)
    try:
        await rt.initialize()
        result = await rt.run("看看项目")
        run = (await rt.store.run("tool_runs", result.turn_id))[0]
        assert run["state"] == "denied" and run["started"] is None
        assert path.read_text() == "user concurrent edit"
    finally:
        await rt.close()


async def test_provider_understanding_does_not_retry_on_429(config):
    import httpx
    from openai import AsyncOpenAI
    from pydantic import SecretStr

    from kaki.provider import RightCodeProvider
    from kaki.types import ProviderError

    config.providers.openai.api_key = SecretStr("test")
    requests = []

    async def transport(request):
        requests.append(request)
        return httpx.Response(429, json={"error": {"message": "limited", "type": "rate_limit"}})

    provider = RightCodeProvider(config)
    await provider.client.close()
    provider.client = AsyncOpenAI(
        api_key="test", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
    )
    try:
        with pytest.raises(ProviderError):
            await provider.complete([Message("user", "task")], kind="understanding", max_attempts=1)
        assert len(requests) == 1
    finally:
        await provider.close()


async def test_tool_catalog_matches_hard_denials_and_read_only_ceiling(workspace, config):
    from kaki.tools import Tool

    config.permissions.mode = "read_only"
    rt = Runtime(workspace, config, provider=ScriptedProvider())
    try:
        await rt.initialize()

        async def noop(args, ctx):
            raise AssertionError("blocked tool must never run")

        rt.tools.add(Tool("kaki_browser_evaluate", "js", {}, [], "EXTERNAL", noop))
        names = {definition["name"] for definition in rt.tools.definitions()}
        assert not names & {"kaki_write_file", "kaki_edit_file", "kaki_remember", "kaki_browser_evaluate"}
        assert "kaki_read_file" in names and "kaki_exec" in names
        denied = await rt.tools.execute(ToolCall("x", "kaki_browser_evaluate", {}), {})
        assert denied.denied and not denied.executed
        prefix, _ = await rt.context.prefix(Message("user", "介绍自己"))
        assert "read_only" in prefix.content and "未开放" in prefix.content
    finally:
        await rt.close()


async def test_uv_test_runner_is_actually_executed(workspace, config, monkeypatch):
    import os

    runner_dir = workspace.root / "runner-bin"
    runner_dir.mkdir()
    runner = runner_dir / "uv"
    runner.write_text("#!/bin/sh\nprintf 'UV_RUNNER_REACHED:%s\\n' \"$*\"\n")
    runner.chmod(0o755)
    monkeypatch.setenv("PATH", str(runner_dir) + os.pathsep + os.environ["PATH"])
    rt = Runtime(workspace, config, provider=ScriptedProvider())
    try:
        await rt.initialize()
        result = await rt.tools.execute(
            ToolCall("uv", "kaki_exec", {"command": "uv run python3 -m unittest -v"}),
            {"human_text": "运行测试"},
        )
        payload = json.loads(result.text)
        assert not result.failed and result.executed
        assert payload["argv"][:2] == ["uv", "run"]
        assert "UV_RUNNER_REACHED:run python3 -m unittest -v" in payload["stdout"]
        with pytest.raises(KakiError):
            structured_command("uv run git status")
    finally:
        await rt.close()
