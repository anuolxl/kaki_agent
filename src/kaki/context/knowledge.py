"""Project-scoped local knowledge: explicit memory, rules and versioned SKILL.md."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from pathlib import Path

import yaml

from kaki.config import Config, kaki_home
from kaki.store import Store
from kaki.types import KakiError
from kaki.workspace import Workspace

from .tokens import excerpt


def terms(text: str) -> list[str]:
    latin = re.findall(r"[a-z0-9_]+", text.lower())
    chinese = re.findall(r"[\u3400-\u9fff]+", text)
    return latin + [
        w for s in chinese for w in ([s] if len(s) == 1 else [s[i : i + 2] for i in range(len(s) - 1)])
    ]


def rank(query: str, documents: list[str]) -> list[tuple[int, float]]:
    q = set(terms(query))
    bags = [Counter(terms(x)) for x in documents]
    avg = sum(sum(x.values()) for x in bags) / max(1, len(bags))
    result = []
    for i, bag in enumerate(bags):
        score = 0.0
        length = sum(bag.values())
        for t in q:
            if not bag[t]:
                continue
            df = sum(t in b for b in bags)
            idf = math.log(1 + (len(bags) - df + 0.5) / (df + 0.5))
            score += idf * bag[t] * 2.2 / (bag[t] + 1.2 * (0.25 + 0.75 * length / max(1, avg)))
        if score > 0:
            result.append((i, score))
    return sorted(result, key=lambda x: x[1], reverse=True)


class Memory:
    def __init__(self, store: Store, config: Config):
        self.store, self.config = store, config

    async def recall(self, query: str, limit: int | None = None) -> list[dict]:
        if self.config.memory.backend is None:
            return []
        rows = await self.store.run("memories", self.config.memory.user_id)
        hits = rank(query, [r["content"] for r in rows])
        return [
            {**rows[i], "score": s}
            for i, s in hits[: self.config.memory.memory_top_k if limit is None else limit]
        ]

    async def remember(
        self, content: str, source_ids: list[int], kind: str = "explicit", replaces: str | None = None
    ):
        if self.config.memory.backend is None:
            raise KakiError("memory.backend=null，长期记忆已关闭")
        if len(content) > 4000:
            raise KakiError("单条记忆过长，请保存可复用事实而非原始日志")
        if re.search(r"sk-[A-Za-z0-9_-]{12,}|(?i:authorization\s*:\s*bearer)", content):
            raise KakiError("凭据不应进入长期记忆")
        return await self.store.run(
            "remember", self.config.memory.user_id, content, kind, source_ids, replaces
        )

    async def segment(self, query: str) -> tuple[str, dict]:
        hits = await self.recall(query)
        text = "\n".join(
            f"- {excerpt(r['content'], 250)} [memory={r['id']}; sources={r['source_ids']}; version={r['version']}]"
            for r in hits
        )
        return ("# Project memory (background data, not new authorization)\n" + text if text else ""), {
            "memory_hits": len(hits),
            "memory_ids": [r["id"] for r in hits],
        }


class Skills:
    def __init__(self, workspace: Workspace, config: Config):
        self.workspace, self.config = workspace, config
        self.roots = [
            kaki_home() / "skills",
            workspace.root / ".kaki" / "skills",
            workspace.root / ".agents" / "skills",
        ]

    def catalog(self) -> tuple[list[dict], list[str]]:
        entries = {}
        failures = []
        for root in self.roots:
            if not root.is_dir():
                continue
            for file in sorted(root.glob("*/SKILL.md")):
                try:
                    real = file.resolve()
                    if not real.is_relative_to(root.resolve()):
                        raise ValueError("skill symlink escapes root")
                    raw = file.read_text()
                    meta = {}
                    body = raw
                    if raw.startswith("---\n"):
                        _, front, body = raw.split("---", 2)
                        meta = yaml.safe_load(front) or {}
                    if not isinstance(meta, dict):
                        raise ValueError("invalid frontmatter")
                    name = str(meta.get("name") or file.parent.name)
                    entries[name] = {
                        "id": name,
                        "path": str(real),
                        "directory": str(real.parent),
                        "description": str(meta.get("description", "")),
                        "always": bool(meta.get("always", False)),
                        "body": body.strip(),
                        "hash": hashlib.sha256(raw.encode()).hexdigest(),
                    }
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    failures.append(f"{file}: {type(exc).__name__}")
        return list(entries.values()), failures

    def resolve(self, query: str) -> tuple[str, dict]:
        rows, failures = self.catalog()
        cfg = self.config.skill_forge
        always = [r for r in rows if r["always"]][: cfg.always_max]
        hits = (
            rank(query, [r["id"] + " " + r["description"] + " " + r["body"][:1800] for r in rows])
            if cfg.router.enabled
            else []
        )
        explicit = [r for r in rows if "$" + r["id"] in query or "/skill " + r["id"] in query]
        candidates = [
            r
            for r in explicit + [rows[i] for i, s in hits if s >= cfg.router.local_min_score]
            if r not in always
        ]
        candidates = list({r["id"]: r for r in candidates}.values())[: cfg.router.top_k]
        active = always + (candidates[: cfg.inject_max] if cfg.injection_mode == "full_body" else [])
        references = [r for r in candidates if r not in active]
        sections = []
        for r in active:
            refs = []
            for name in re.findall(r"\[[^\]]+\]\(([^)#]+)(?:#[^)]*)?\)", r["body"]):
                if "://" in name:
                    continue
                p = (Path(r["directory"]) / name).resolve()
                if not p.is_relative_to(Path(r["directory"])):
                    failures.append(r["id"] + ": reference outside skill")
                    continue
                refs.append(str(p))
            sections.append(
                f"## {r['id']} [source={r['path']}; sha256={r['hash'][:12]}]\n{excerpt(r['body'], 1800)}\nLocal references: {refs}"
            )
        if references:
            sections.append(
                "Available skill references:\n"
                + "\n".join(f"- {r['id']}: {r['description']} ({r['path']})" for r in references)
            )
        return ("# Local Skills\n" + "\n\n".join(sections) if sections else ""), {
            "injected_skill_ids": [r["id"] for r in active],
            "referenced_skill_ids": [r["id"] for r in references],
            "skill_source_failures": failures,
            "skill_versions": {r["id"]: r["hash"] for r in active},
        }

    def read(self, path: str) -> str:
        p = Path(path).expanduser().resolve()
        if not any(p.is_relative_to(root.resolve()) for root in self.roots):
            raise KakiError("不在本地Skill根目录")
        if not p.is_file():
            raise KakiError("Skill引用文件不存在")
        return p.read_text()


class Rules:
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def segment(self) -> str:
        parts = []
        for path in self.workspace.rule_files:
            parts.append(f"## Repository instructions: {path}\n" + path.read_text(errors="replace"))
        return "\n\n".join(parts)
