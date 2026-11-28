import os
import subprocess

import pytest

from kaki.checkpoint import Checkpoint
from kaki.store import Store


@pytest.mark.asyncio
async def test_shadow_preserves_bytes_index_and_conflicts(workspace, config):
    root = workspace.root
    (root / "raw.txt").write_bytes(b"first\r\nsecond\r\n")
    (root / "binary.bin").write_bytes(b"\x00\xff\x05")
    (root / "removed.txt").write_text("restore me")
    (root / "link").symlink_to("raw.txt")
    (root / "run.sh").write_text("echo yes\n")
    (root / "run.sh").chmod(0o755)
    (root / "mode-user.sh").write_text("before")
    (root / "mode-user.sh").chmod(0o755)
    (root / ".env").write_text("SECRET=no-store")
    subprocess.run(["git", "-C", str(root), "add", "raw.txt"], check=True)
    index_before = (root / ".git/index").read_bytes()
    head_before = (root / ".git/HEAD").read_bytes()
    s = Store(workspace.state / "test.sqlite3", workspace.id)
    await s.open()
    try:
        checkpoint = Checkpoint(workspace, s, config)
        before = await checkpoint.capture("turn", "before")
        assert before["state"] == "ready", before
        tree = await checkpoint.tree(before["commit"])
        assert ".env" not in tree and ".git/HEAD" not in tree
        (root / "raw.txt").write_text("changed")
        (root / "binary.bin").write_bytes(b"change")
        (root / "removed.txt").unlink()
        (root / "link").unlink()
        (root / "link").symlink_to("binary.bin")
        (root / "run.sh").write_text("echo no\n")
        (root / "mode-user.sh").write_text("after")
        (root / "added.txt").write_text("new")
        after = await checkpoint.capture("turn", "after")
        assert after["state"] == "ready", after
        (root / "raw.txt").write_text("user edit after turn")
        (root / "mode-user.sh").chmod(0o644)
        plan = await checkpoint.plan_restore("turn")
        assert next(x for x in plan["paths"] if x["path"] == "raw.txt")["conflict"]
        restored = await checkpoint.apply(plan, [x["path"] for x in plan["paths"]])
        assert restored["conflicts"] == ["mode-user.sh", "raw.txt"]
        assert (root / "mode-user.sh").read_text() == "after"
        assert not (root / "mode-user.sh").stat().st_mode & 0o111
        assert (root / "raw.txt").read_text() == "user edit after turn"
        assert (root / "binary.bin").read_bytes() == b"\x00\xff\x05"
        assert (root / "removed.txt").read_text() == "restore me"
        assert os.readlink(root / "link") == "raw.txt"
        assert (root / "run.sh").stat().st_mode & 0o111
        assert not (root / "added.txt").exists()
        assert (root / ".git/index").read_bytes() == index_before
        assert (root / ".git/HEAD").read_bytes() == head_before
    finally:
        await s.close()


async def test_capture_rebuilds_shadow_index_after_new_nested_repo_exclusion(workspace, config):
    root = workspace.root
    (root / "source.txt").write_text("source")
    subprocess.run(["git", "-C", str(root), "add", "source.txt"], check=True)
    original_index = (root / ".git/index").read_bytes()
    original_head = (root / ".git/HEAD").read_bytes()
    nested = root / ".worktrees" / "other-task"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(nested)], check=True)
    (nested / "user.txt").write_text("unrelated task")
    subprocess.run(["git", "-C", str(nested), "add", "user.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(nested),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "nested fixture",
        ],
        check=True,
    )
    store = Store(workspace.state / "nested.sqlite3", workspace.id)
    await store.open()
    try:
        checkpoint = Checkpoint(workspace, store, config)
        await checkpoint.ensure()
        await checkpoint.git("add", "-f", "--", ".worktrees/other-task")
        _, indexed, _ = await checkpoint.git("ls-files", "--stage")
        assert b"160000" in indexed
        result = await checkpoint.capture("turn", "before")
        assert result["state"] == "ready", result
        tree = await checkpoint.tree(result["commit"])
        assert "source.txt" in tree and not any(p.startswith(".worktrees/") for p in tree)
        assert (nested / "user.txt").read_text() == "unrelated task"
        assert (root / ".git/index").read_bytes() == original_index
        assert (root / ".git/HEAD").read_bytes() == original_head
    finally:
        await store.close()
