"""Independent raw-byte shadow Git snapshots; never use the user's Git index."""

from __future__ import annotations

import asyncio
import hashlib
import os

from kaki.config import Config
from kaki.types import KakiError, uid
from kaki.workspace import Workspace

EXCLUDES = """
.git
.kaki/
.venv/
venv/
__pycache__/
*.pyc
node_modules/
build/
dist/
target/
.idea/
.worktrees/
.DS_Store
.env
.env.*
*.pem
*.key
*.p12
secrets.yaml
*.log
"""


class Checkpoint:
    # 独立 shadow Git 快照；git-dir 和 index 在项目 state 内，不能改用户仓库的索引/分支。
    def __init__(self, workspace: Workspace, store, config: Config):
        self.workspace, self.store, self.config = workspace, store, config
        # 路径必须位于当前项目 state；所有影子 Git 命令都显式使用它。
        self.git_dir = (workspace.state / config.runtime.checkpoint.shadow_dir).resolve()
        if self.git_dir == workspace.state or not self.git_dir.is_relative_to(workspace.state):
            raise KakiError("shadowDir必须在当前项目状态目录内")
        self.ready = False
        # 仅用于定期触发影子仓 gc，不代表业务 Git 提交次数。
        self.commits = 0

    async def git(self, *args, check=True) -> tuple[int, bytes, bytes]:
        # 所有快照命令强制指定影子 git-dir，禁用户 hooks/filters；工作树只是待读取的数据源。
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"}}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
        command = [
            "git",
            f"--git-dir={self.git_dir}",
            *([] if args and args[0] == "init" else [f"--work-tree={self.workspace.root}"]),
            "-c",
            "user.name=Kaki",
            "-c",
            "user.email=checkpoint@kaki.local",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.quotePath=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ]
        p = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.workspace.root,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(p.communicate(), 30)
        except BaseException:
            if p.returncode is None:
                p.kill()
                await p.wait()
            raise
        if check and p.returncode:
            raise KakiError("工作区快照失败：" + err.decode(errors="replace")[:500])
        return p.returncode, out, err

    async def ensure(self):
        # 准备影子仓与排除/原始字节规则，避免 CRLF、二进制或用户过滤器改变保存内容。
        if self.ready:
            return
        self.git_dir.parent.mkdir(parents=True, exist_ok=True)
        await self.git("init", "--bare", str(self.git_dir))
        info = self.git_dir / "info"
        info.mkdir(exist_ok=True)
        (info / "exclude").write_text(EXCLUDES)
        # Highest-priority attributes preserve CRLF/binary bytes and prohibit user-defined filters.
        (info / "attributes").write_text("* -text -filter -ident\n")
        await self.git("config", "core.autocrlf", "false")
        self.ready = True

    async def capture(self, turn: str, phase: str) -> dict:
        # 先登记 creating，再生成树和 commit；失败保持 failed，不能冒充可自动恢复。
        identity = uid("snap_")
        metadata = {
            "root": str(self.workspace.root),
            "excludes_sha256": hashlib.sha256(EXCLUDES.encode()).hexdigest(),
        }
        await self.store.run("snapshot", identity, turn, phase, "creating", None, metadata)
        try:
            await self.ensure()
            # Exclusions can change between installed versions. Rebuild only our
            # shadow index so previously indexed ignored Gitlinks cannot leak in.
            await self.git("read-tree", "--empty")
            await self.git("add", "-A", "--", ".")
            _, staged, _ = await self.git("ls-files", "--stage", "-z")
            if any(x.startswith(b"160000 ") for x in staged.split(b"\0") if x):
                raise KakiError("快照发现嵌套Git单元；v0.1不把Gitlink当文件内容覆盖")
            rc, head, _ = await self.git("rev-parse", "--verify", "HEAD", check=False)
            changed, _, _ = await self.git("diff", "--cached", "--quiet", check=False)
            if rc or changed:
                await self.git("commit", "--allow-empty", "-m", f"Kaki {turn} [{phase}]")
                _, head, _ = await self.git("rev-parse", "HEAD")
                self.commits += 1
                if self.commits % 50 == 0:
                    await self.git("gc", "--auto")
            commit = head.decode().strip()
            metadata["unchanged"] = not bool(rc or changed)
            await self.store.run("snapshot", identity, turn, phase, "ready", commit, metadata)
            return {"id": identity, "commit": commit, "state": "ready", "phase": phase}
        except asyncio.CancelledError:
            await asyncio.shield(
                self.store.run(
                    "snapshot", identity, turn, phase, "failed", None, {**metadata, "error": "cancelled"}
                )
            )
            raise
        except Exception as exc:
            metadata["error"] = str(exc)
            await self.store.run("snapshot", identity, turn, phase, "failed", None, metadata)
            return {"id": identity, "commit": None, "state": "failed", "phase": phase, "error": str(exc)}

    async def tree(self, commit: str) -> dict[str, dict]:
        # 读取指定快照树的对象 ID 与文件模式，供恢复预览对比。
        if not commit or not all(c in "0123456789abcdef" for c in commit):
            raise KakiError("非法快照提交ID")
        _, raw, _ = await self.git("ls-tree", "-rz", commit)
        result = {}
        for line in raw.split(b"\0"):
            if not line:
                continue
            info, path = line.split(b"\t", 1)
            mode, kind, oid = info.decode().split()
            result[os.fsdecode(path)] = {"mode": mode, "oid": oid, "kind": kind}
        return result

    def current_oid(self, path: str) -> str | None:
        # 按 Git blob 规则计算当前原始字节 ID，不依赖用户 index 的暂存状态。
        p = self.workspace.root / path
        if p.is_symlink():
            raw = os.fsencode(os.readlink(p))
        elif p.is_file():
            raw = p.read_bytes()
        else:
            return None
        return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()

    def current_mode(self, path: str) -> str | None:
        # 恢复需要同时比较类型/可执行位；内容没变也不能覆盖用户后改的权限。
        p = self.workspace.root / path
        if p.is_symlink():
            return "120000"
        if p.is_file():
            return "100755" if p.stat().st_mode & 0o111 else "100644"
        return "040000" if p.exists() else None

    async def plan_restore(self, turn: str) -> dict:
        # 先形成 before/after 到当前工作树的冲突清单；预览本身不是写入授权。
        snapshots = await self.store.run("snapshots", turn)
        before = next((s for s in snapshots if s["phase"] == "before" and s["state"] == "ready"), None)
        after = next(
            (s for s in reversed(snapshots) if s["phase"] == "after" and s["state"] == "ready"), None
        )
        if not before or not after:
            raise KakiError("该任务没有完整before/after快照，不能安全自动恢复")
        a = await self.tree(before["commit_id"])
        b = await self.tree(after["commit_id"])
        paths = []
        for path in sorted(set(a) | set(b)):
            if a.get(path) == b.get(path):
                continue
            current = await asyncio.to_thread(self.current_oid, path)
            expected = b.get(path, {}).get("oid")
            mode = await asyncio.to_thread(self.current_mode, path)
            expected_mode = b.get(path, {}).get("mode")
            paths.append(
                {
                    "path": path,
                    "before": a.get(path),
                    "after": b.get(path),
                    "expected_current_oid": expected,
                    "current_oid": current,
                    "current_mode": mode,
                    "expected_current_mode": expected_mode,
                    "conflict": current != expected or mode != expected_mode,
                }
            )
        return {
            "turn_id": turn,
            "before_snapshot": before["id"],
            "after_snapshot": after["id"],
            "paths": paths,
        }

    async def apply(self, plan: dict, approved_paths: list[str]) -> dict:
        # 仅恢复已批准路径，写入前再次校验当前内容和模式，避免覆盖任务后的外部编辑。
        allowed = {x["path"]: x for x in plan["paths"]}
        restored = []
        conflicts = []
        for name in approved_paths:
            item = allowed.get(name)
            if not item:
                raise KakiError("恢复选择不属于此计划")
            # Recheck at write time: a preview alone is not permission to clobber subsequent edits.
            if (
                await asyncio.to_thread(self.current_oid, name) != item["expected_current_oid"]
                or await asyncio.to_thread(self.current_mode, name) != item["expected_current_mode"]
            ):
                conflicts.append(name)
                continue
            p = self.workspace.root / name
            parent = p.parent.resolve()
            if not parent.is_relative_to(self.workspace.root):
                raise KakiError("恢复路径的父目录越界")
            target = item["before"]
            if p.is_symlink():
                p.unlink()
            if target is None:
                if p.is_file():
                    p.unlink()
            else:
                _, raw, _ = await self.git("cat-file", "blob", target["oid"])
                p.parent.mkdir(parents=True, exist_ok=True)
                if target["mode"] == "120000":
                    if p.exists():
                        p.unlink()
                    p.symlink_to(os.fsdecode(raw))
                else:
                    temp = p.parent / uid(".kaki-restore-")
                    temp.write_bytes(raw)
                    temp.chmod(0o755 if target["mode"] == "100755" else 0o644)
                    temp.replace(p)
            restored.append(name)
        return {"restored": restored, "conflicts": conflicts}
