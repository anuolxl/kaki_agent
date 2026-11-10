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
