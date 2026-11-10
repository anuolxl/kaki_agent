"""Single-Git workspace boundary, immutable artifacts and an OS execution lease."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from kaki.config import Config, kaki_home
from kaki.types import KakiError, uid


@dataclass
class Workspace:
    root: Path
    state: Path
    id: str
    knowledge_roots: list[Path] = field(default_factory=list)
    rule_files: list[Path] = field(default_factory=list)

    @classmethod
    def resolve(cls, cwd: Path | None, config: Config) -> Workspace:
        chosen = (cwd or Path(config.agents.defaults.workspace or Path.cwd())).expanduser().resolve()
        if not chosen.is_dir():
            raise KakiError(f"工作目录不存在：{chosen}")
        r = subprocess.run(
            ["git", "-C", str(chosen), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode:
            raise KakiError("--cwd需要指向单个Git仓库；先在目标目录git init")
        root = Path(r.stdout.strip()).resolve()
        if (root / ".gitmodules").exists() and (root / ".gitmodules").read_text().strip():
            raise KakiError("当前目录含Git子模块；v0.1请指定media_platform等单Git子仓")
        slug = re.sub(r"[^A-Za-z0-9._-]", "-", root.name) or "workspace"
        identity = hashlib.sha256(os.fsencode(root)).hexdigest()[:16]
        state = kaki_home() / "projects" / f"{slug}-{identity}"
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        ws = cls(root, state, identity)
        pending = [root / name for name in ["AGENTS.md", "KAKI.md"]]
        seen = set()
        while pending and len(seen) < 16:
            p = pending.pop(0).resolve()
            if p in seen or not p.is_file():
                continue
            seen.add(p)
            ws.rule_files.append(p)
            # Explicit inherited rule documents are read-only knowledge, not a write grant.
            if p.parent != root and (p.parent / "docs").is_dir():
                ws.knowledge_roots.append((p.parent / "docs").resolve())
            for match in re.finditer(r"\]\(([^)]+(?:AGENTS|KAKI)\.md)\)", p.read_text(errors="replace")):
                target = (p.parent / match[1]).resolve()
                if target.name in {"AGENTS.md", "KAKI.md"} and target in (
                    root / target.name,
                    *root.parents[0].glob(target.name),
                ):
                    pending.append(target)
        (state / "workspace.json").write_text(
            json.dumps({"workspace_id": identity, "root": str(root)}, ensure_ascii=False)
        )
        return ws

    def path(self, value: str, *, write: bool = False) -> Path:
        p = Path(value).expanduser()
        p = (self.root / p if not p.is_absolute() else p).resolve()
        if not p.is_relative_to(self.root):
            if not write and (
                p in self.rule_files
                or any(p.is_relative_to(x) and p.suffix == ".md" for x in self.knowledge_roots)
            ):
                return p
            raise KakiError("路径超出工作区边界")
        rel = p.relative_to(self.root)
        if ".git" in rel.parts:
            raise KakiError("工具不能访问Git内部元数据")
        if any(x in {".env", "credentials"} or x.endswith((".pem", ".key", ".p12")) for x in rel.parts):
            raise KakiError("拒绝通过Agent工具读取或修改凭据文件")
        if p == self.root:
            return p
        for parent in [p.parent, *p.parents]:
            if not parent.is_relative_to(self.root):
                break
            if parent == self.root:
                break
            if (parent / ".git").exists():
                raise KakiError("不能跨入嵌套Git仓库")
        return p

    def artifact(self, content: bytes, *, media_type: str = "text/plain", suffix: str = ".txt") -> dict:
        digest = hashlib.sha256(content).hexdigest()
        folder = self.state / "artifacts"
        folder.mkdir(exist_ok=True, mode=0o700)
        target = folder / (digest + suffix)
        if not target.exists():
            temp = folder / (uid("tmp-") + suffix)
            temp.write_bytes(content)
            os.chmod(temp, 0o600)
            os.replace(temp, target)
        return {
            "artifact_id": digest + suffix,
            "sha256": digest,
            "size": len(content),
            "media_type": media_type,
            "path": str(target),
        }

    def artifact_path(self, identity: str) -> Path:
        p = (self.state / "artifacts" / identity).resolve()
        if not p.is_relative_to(self.state / "artifacts") or not p.is_file():
            raise KakiError("附件不存在或引用非法")
        return p


class WorkspaceLease:
    def __init__(self, state: Path):
        self.path = state / "execution.lock"
        self.file = None

    def acquire(self):
        self.file = self.path.open("a+")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            self.file = None
            raise KakiError("另一个Kaki任务正在写此工作区，请等待或取消该任务") from None

    def release(self):
        if self.file:
            fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None
