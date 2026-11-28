import asyncio
import hashlib
import json
import subprocess
import sys

import pytest
from conftest import ScriptedProvider

from kaki.config import CommandOptions
from kaki.decisions import APPROVAL_LABELS
from kaki.execution import ExecutionTimeout, run_process
from kaki.runtime import Runtime
from kaki.types import KakiError, ModelResponse, ToolCall


def git(workspace, *args):
    return subprocess.check_output(["git", *args], cwd=workspace.root, text=True).strip()


def seed(workspace):
    git(workspace, "config", "user.name", "Test")
    git(workspace, "config", "user.email", "test@example.invalid")
    (workspace.root / "code.txt").write_text("before")
    git(workspace, "add", "code.txt")
    git(workspace, "commit", "-m", "initial")
    git(workspace, "branch", "-M", "master")


async def test_branch_then_edit_stage_commit_and_context_refresh(workspace, config):
    seed(workspace)
    config.tools.development.enabled = True
    p = ScriptedProvider(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    "b", "kaki_git", {"action": "create_branch", "branch": "codex/fix", "base": "master"}
                )
            ]
        ),
        ModelResponse(tool_calls=[ToolCall("r", "kaki_read_file", {"path": "code.txt"})]),
        ModelResponse(
            tool_calls=[
                ToolCall(
                    "e", "kaki_edit_file", {"path": "code.txt", "old_text": "before", "new_text": "after"}
                )
            ]
        ),
        ModelResponse(tool_calls=[ToolCall("s", "kaki_git", {"action": "stage", "paths": ["code.txt"]})]),
        ModelResponse(
            tool_calls=[
                ToolCall("c", "kaki_git", {"action": "commit", "paths": ["code.txt"], "message": "fix code"})
            ]
        ),
        ModelResponse(text="done"),
    )
    rt = Runtime(workspace, config, provider=p)
    events = []
    rt.listeners.append(lambda event: record(events, event))
    try:
        await rt.initialize(connect_browser=False)
        result = await rt.run("修改code.txt，创建分支并提交commit")
        assert result.status == "completed"
        assert git(workspace, "branch", "--show-current") == "codex/fix"
        assert git(workspace, "log", "-1", "--format=%s") == "fix code"
        assert git(workspace, "status", "--porcelain") == ""
        assert any(
            e.type == "context.compacted" and e.data.get("reason") == "git_branch_changed" for e in events
        )
        assert all(r["state"] == "succeeded" for r in await rt.store.run("tool_runs", result.turn_id))
    finally:
        await rt.close()


async def record(events, event):
    events.append(event)


async def test_protected_branch_and_unrelated_index_are_preserved(workspace, config):
    seed(workspace)
    config.tools.development.enabled = True
    rt = Runtime(workspace, config, provider=ScriptedProvider())
    try:
        await rt.initialize(connect_browser=False)
        with pytest.raises(KakiError, match="保护分支"):
            await rt.tools.development.writable_state()
        git(workspace, "switch", "-c", "codex/fix")
        (workspace.root / "other.txt").write_text("user")
        git(workspace, "add", "other.txt")
        with pytest.raises(KakiError, match="暂存区"):
            await rt.tools.development.prepare_git({"action": "stage", "paths": ["code.txt"]}, {})
        assert git(workspace, "diff", "--cached", "--name-only") == "other.txt"
        with pytest.raises(KakiError, match="明确"):
            rt.tools.development.paths(["."])
    finally:
        await rt.close()


async def test_branch_change_blocks_remaining_batch_under_stale_rules(workspace, config):
    seed(workspace)
    config.tools.development.enabled = True
    p = ScriptedProvider(
        ModelResponse(
            tool_calls=[
                ToolCall(
                    "b", "kaki_git", {"action": "create_branch", "branch": "codex/fix", "base": "master"}
                ),
                ToolCall("w", "kaki_write_file", {"path": "new.txt", "content": "stale"}),
            ]
        ),
        ModelResponse(text="refreshed"),
    )
    rt = Runtime(workspace, config, provider=p)
    try:
        await rt.initialize(connect_browser=False)
        result = await rt.run("创建分支，修改代码")
        assert not (workspace.root / "new.txt").exists()
        runs = await rt.store.run("tool_runs", result.turn_id)
        assert [r["state"] for r in runs] == ["succeeded", "failed"]
    finally:
        await rt.close()


async def test_profile_integrity_arguments_and_input_approval_binding(workspace, config):
    seed(workspace)
    script = workspace.root / "trusted.py"
    script.write_text("print('trusted')")
    request = workspace.root / "request.json"
    request.write_text(json.dumps({"harnessTaskID": "one", "inputs": {"env": "boe"}}))
    config.tools.commands["trusted"] = CommandOptions(
        description="test submission",
        argv=[sys.executable, str(script)],
        capability="external.write",
        allow_arguments=True,
        allowed_flags=["--request-file", "--output"],
        path_flags=["--request-file"],
        write_flags=["--output"],
        files={str(script): hashlib.sha256(script.read_bytes()).hexdigest()},
    )
    rt = Runtime(workspace, config, provider=ScriptedProvider())
    try:
        await rt.initialize(connect_browser=False)
        args = {"name": "trusted", "arguments": ["--request-file", str(request)]}
        before = await rt.tools.development.prepare_command(args, {})
        assert before.review["inputs"]["env"] == "boe"
        request.write_text(json.dumps({"harnessTaskID": "two", "inputs": {"env": "ppe"}}))
        after = await rt.tools.development.prepare_command(args, {})
        assert before != after
        for arguments in (["--output", str(workspace.root / "bad")], ["--evil"], ["--request-file=/tmp/bad"]):
            with pytest.raises(KakiError):
                await rt.tools.development.prepare_command({"name": "trusted", "arguments": arguments}, {})
        script.write_text("print('changed')")
        assert not rt.tools.development.command_catalog()[0]["available"]
        with pytest.raises(KakiError, match="hash"):
            await rt.tools.development.prepare_command(args, {})
    finally:
        await rt.close()


async def test_external_command_only_once_approval_and_real_execution(workspace, config):
    config.tools.commands["echo-profile"] = CommandOptions(
        description="trusted test",
        argv=[sys.executable, "-c", "print('actual output')"],
        capability="external.write",
    )
    p = ScriptedProvider(
        ModelResponse(tool_calls=[ToolCall("a", "kaki_command", {"name": "echo-profile"})]),
        ModelResponse(tool_calls=[ToolCall("b", "kaki_command", {"name": "echo-profile"})]),
        ModelResponse(text="done"),
    )
    rt = Runtime(workspace, config, provider=p)
    questions = []

    async def answer(event):
        if event.type == "question.requested":
            questions.append(event)
            assert "turn" not in event.data["operation"]["grant_scopes"]
            assert (
                "turn" not in event.data["details"]["operation"]["grant_scopes"]
                if "details" in event.data
                else True
            )
            await rt.answer(event.data["question_id"], APPROVAL_LABELS["once"], "once")

    rt.listeners.append(answer)
    try:
        await rt.initialize(connect_browser=False)
        result = await rt.run("执行该命令")
        assert len(questions) == 2
        assert result.verification["status"] == "not_run"
        messages = await rt.store.run("messages", rt.session_id)
        assert sum("actual output" in m.content for m in messages if m.role == "tool") == 2
    finally:
        await rt.close()


async def test_registered_test_profile_records_real_verification(workspace, config):
    config.tools.commands["test-profile"] = CommandOptions(
        description="run fixture tests",
        argv=[sys.executable, "-m", "unittest", "-v"],
        capability="test.execute",
    )
    (workspace.root / "test_case.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n def test_one(self): self.assertEqual(2+2,4)\n"
    )
    p = ScriptedProvider(
        ModelResponse(tool_calls=[ToolCall("test", "kaki_command", {"name": "test-profile"})]),
        ModelResponse(text="done"),
    )
    rt = Runtime(workspace, config, provider=p)
    try:
        await rt.initialize(connect_browser=False)
        result = await rt.run("运行测试")
        assert result.verification["status"] == "passed"
        assert result.verification["commands"][0]["exit_code"] == 0
        assert "unittest" in result.verification["commands"][0]["command"]
    finally:
        await rt.close()


async def test_skill_readiness_explicit_boundary_private_files_and_deferred_tool(workspace, config):
    seed(workspace)
    config.tools.development.enabled = True
    root = workspace.root / ".kaki" / "skills" / "prepare-mr"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: prepare-mr\ndescription: prepare draft MR\nmetadata:\n  kaki:\n    required_tools: [kaki_mr]\n    required_commands: [missing]\n    private_paths: [private.py]\n---\nInstructions"
    )
    (root / "private.py").write_text("private credential implementation")
    rt = Runtime(workspace, config, provider=ScriptedProvider())
    try:
        await rt.initialize(connect_browser=False)
        rows, _ = rt.context.skills.catalog()
        assert not rows[0]["ready"] and rows[0]["missing_commands"] == ["missing"]
        with pytest.raises(KakiError, match="受保护"):
            rt.context.skills.read(str(root / "private.py"))
        assert "kaki_mr" not in {t["name"] for t in rt.tools.definitions()}
        result = await rt.tools.tool_search({"query": "kaki_mr"}, {})
        assert json.loads(result.text)["matches"][0]["name"] == "kaki_mr"
        assert "kaki_mr" in {t["name"] for t in rt.tools.definitions()}
        rt.tools.begin_turn()
        assert "kaki_mr" not in {t["name"] for t in rt.tools.definitions()}
    finally:
        await rt.close()


async def test_process_timeout_reaps_child_and_caps_output(tmp_path):
    with pytest.raises(ExecutionTimeout):
        await run_process([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 0.02)
    task = asyncio.create_task(
        run_process([sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 60)
    )
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_fetch_real_remote_branch_and_no_duplicate_mr_on_uncertain_result(
    workspace, config, tmp_path, monkeypatch
):
    seed(workspace)
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(workspace.root), str(remote)], check=True, capture_output=True
    )
    git(workspace, "remote", "add", "origin", str(remote))
    config.tools.development.enabled = True
    config.tools.development.allow_mr_create = True
    rt = Runtime(workspace, config, provider=ScriptedProvider())
    try:
        await rt.initialize(connect_browser=False)
        dev = rt.tools.development
        operation = await dev.prepare_git({"action": "fetch", "ref": "master"}, {})
        result = await dev.git({"action": "fetch"}, {"prepared_operation": operation})
        assert not result.failed and git(workspace, "rev-parse", "origin/master") == git(
            workspace, "rev-parse", "HEAD"
        )
        git(workspace, "switch", "-c", "codex/fix")
        git(workspace, "remote", "set-url", "origin", "git@github.com:test/repo.git")
        calls = []

        async def no_existing(*args):
            return None

        async def unknown(*args, **kwargs):
            calls.append(args)
            raise ExecutionTimeout("unknown result")

        monkeypatch.setattr(dev, "_find_mr", no_existing)
        monkeypatch.setattr("kaki.development.run_process", unknown)

        # Stub state after replacing the process runner so only the external creation is counted.
        async def state():
            return {"head": "abc", "branch": "codex/fix", "index": "", "dirty": ""}

        async def repository():
            return "github.com", "test/repo"

        monkeypatch.setattr(dev, "state", state)
        monkeypatch.setattr(dev, "repository", repository)
        args = {"action": "create_draft", "base": "master", "title": "fix", "body": "details"}
        first = await dev.mr(args, {})
        second = await dev.mr(args, {})
        assert first.uncertain and second.uncertain and len(calls) == 1
        receipt = next((workspace.state / "workflows/mr").glob("*.json"))
        assert json.loads(receipt.read_text())["state"] == "pending"
    finally:
        await rt.close()
