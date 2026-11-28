"""Project-scoped local knowledge: explicit memory, rules and versioned SKILL.md."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from pathlib import Path

import yaml

from kaki.config import Config, kaki_home, redact_text
from kaki.store import Store
from kaki.types import KakiError
from kaki.workspace import Workspace

from .tokens import excerpt


def terms(text: str) -> list[str]:
    # 本地词法检索分词：Latin 标识符加中文二元词，不是大模型 embedding 或向量数据库。
    latin = re.findall(r"[a-z0-9_]+", text.lower())
    chinese = re.findall(r"[\u3400-\u9fff]+", text)
    return latin + [
        w for s in chinese for w in ([s] if len(s) == 1 else [s[i : i + 2] for i in range(len(s) - 1)])
    ]


def rank(query: str, documents: list[str]) -> list[tuple[int, float]]:
    # 为有限本地文档计算 BM25 排序，保持可解释召回；分数不是执行正确性的证明。
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
    # 项目/用户范围的显式记忆；读取可检索，新增必须有真实来源和用户记忆意图。
    def __init__(self, store: Store, config: Config):
        self.store, self.config = store, config

    async def recall(self, query: str, limit: int | None = None, queries=None) -> list[dict]:
        # 在当前 Store 范围召回 active 版本，原文与派生检索词融合，不自动跨项目读取。
        if self.config.memory.backend is None:
            return []
        rows = await self.store.run("memories", self.config.memory.user_id)
        from kaki.understanding.retrieval import fused_rank

        hits = fused_rank(queries or [query], [r["content"] for r in rows])
        return [
            {**rows[i], "score": s}
            for i, s in hits[: self.config.memory.memory_top_k if limit is None else limit]
        ]

    async def remember(
        self, content: str, source_ids: list[int], kind: str = "explicit", replaces: str | None = None
    ):
        # 限制单条长度、来源与凭据样式；替代保留历史版本，不做静默覆盖。
        if self.config.memory.backend is None:
            raise KakiError("memory.backend=null，长期记忆已关闭")
        if len(content) > 4000:
            raise KakiError("单条记忆过长，请保存可复用事实而非原始日志")
        if re.search(r"sk-[A-Za-z0-9_-]{12,}|(?i:authorization\s*:\s*bearer)", content):
            raise KakiError("凭据不应进入长期记忆")
        return await self.store.run(
            "remember", self.config.memory.user_id, content, kind, source_ids, replaces
        )

    async def segment(self, query: str, queries=None) -> tuple[str, dict]:
        # 把少量带来源的记忆作为提示数据，不把旧记忆当本轮操作批准。
        hits = await self.recall(query, queries=queries)
        text = "\n".join(
            f"- {excerpt(r['content'], 250)} [memory={r['id']}; sources={r['source_ids']}; version={r['version']}]"
            for r in hits
        )
        return ("# Project memory (background data, not new authorization)\n" + text if text else ""), {
            "memory_hits": len(hits),
            "memory_ids": [r["id"] for r in hits],
        }


class Skills:
    # 按本机/项目目录管理 Skill；正文、依赖就绪和执行权限是不同层。
    def __init__(self, workspace: Workspace, config: Config):
        self.workspace, self.config = workspace, config
        self.roots = [
            kaki_home() / "skills",
            workspace.root / ".kaki" / "skills",
            workspace.root / ".agents" / "skills",
        ]
        self.available_tools = None
        self.available_commands = None

    def catalog(self) -> tuple[list[dict], list[str]]:
        # 读取 frontmatter 并按后根覆盖同名项；缺工具/profile 的 Skill 明确标记 unavailable。
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
                    metadata = meta.get("metadata") or {}
                    kaki = metadata.get("kaki", {}) if isinstance(metadata, dict) else {}
                    if not isinstance(kaki, dict):
                        raise ValueError("invalid kaki metadata")
                    private_paths = kaki.get("private_paths", [])
                    if not isinstance(private_paths, list) or any(
                        not isinstance(v, str) for v in private_paths
                    ):
                        raise ValueError("invalid private_paths")
                    required_tools = kaki.get("required_tools", [])
                    required_commands = kaki.get("required_commands", [])
                    if not isinstance(required_commands, list) or any(
                        not isinstance(v, str) for v in required_commands
                    ):
                        raise ValueError("invalid required_commands")
                    if not isinstance(required_tools, list) or any(
                        not isinstance(v, str) for v in required_tools
                    ):
                        raise ValueError("invalid required_tools")
                    available = self.available_tools() if self.available_tools else None
                    missing = [
                        name for name in required_tools if available is not None and name not in available
                    ]
                    commands = self.available_commands() if self.available_commands else None
                    missing_commands = [
                        n for n in required_commands if commands is not None and n not in commands
                    ]
                    entries[name] = {
                        "id": name,
                        "path": str(real),
                        "directory": str(real.parent),
                        "description": str(meta.get("description", "")),
                        "always": bool(meta.get("always", False)),
                        "body": redact_text(body.strip()),
                        "hash": hashlib.sha256(raw.encode()).hexdigest(),
                        "required_tools": required_tools,
                        "required_commands": required_commands,
                        "private_paths": private_paths,
                        "ready": not missing and not missing_commands,
                        "missing_commands": missing_commands,
                        "missing_tools": missing,
                    }
                except (OSError, ValueError, yaml.YAMLError) as exc:
                    failures.append(f"{file}: {type(exc).__name__}")
        return list(entries.values()), failures

    def resolve(self, query: str, queries=None) -> tuple[str, dict]:
        # 显式原文调用优先，词法融合召回少量正文，其余按需读，控制窗口开销。
        rows, failures = self.catalog()
        cfg = self.config.skill_forge
        unavailable = [r for r in rows if not r["ready"]]
        rows = [r for r in rows if r["ready"]]
        always = [r for r in rows if r["always"]][: cfg.always_max]
        from kaki.understanding.retrieval import fused_rank

        hits = (
            fused_rank(
                queries or [query],
                [r["id"] + " " + r["description"] + " " + r["body"][:1800] for r in rows],
                min_score=cfg.router.local_min_score,
            )
            if cfg.router.enabled
            else []
        )
        explicit = [
            r for r in rows if re.search(r"(?:\$|/skill\s+)" + re.escape(r["id"]) + r"(?![\w-])", query)
        ]
        candidates = [r for r in explicit + [rows[i] for i, _ in hits] if r not in always]
        candidates = list({r["id"]: r for r in candidates}.values())[: cfg.router.top_k]
        active = always + (candidates[: cfg.inject_max] if cfg.injection_mode == "full_body" else [])
        references = [r for r in candidates if r not in active]
        sections = (
            [
                "Skill catalog (metadata only; use kaki_skill_read for full instructions):\n"
                + "\n".join(
                    f"- {r['id']}: {r['description'][:220]} [path={r['path']}; requires={r['required_tools']}]"
                    for r in rows[: cfg.catalog_max]
                )
            ]
            if rows
            else []
        )
        if unavailable:
            sections.append(
                "Unavailable Skills (do not pretend they can execute):\n"
                + "\n".join(
                    f"- {r['id']}: missing tools={r['missing_tools']}, commands={r['missing_commands']}"
                    for r in unavailable[:20]
                )
            )
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
            "skill_required_tools": {r["id"]: r["required_tools"] for r in active},
            "unavailable_skills": [
                {
                    "id": r["id"],
                    "missing_tools": r["missing_tools"],
                    "missing_commands": r["missing_commands"],
                }
                for r in unavailable
            ],
        }

    def read(self, path: str) -> str:
        # 引用必须位于 Skill 根且不能是 private_paths/凭据文件，避免把受保护实现送模型。
        p = Path(path).expanduser().resolve()
        if not any(p.is_relative_to(root.resolve()) for root in self.roots):
            raise KakiError("不在本地Skill根目录")
        if not p.is_file():
            raise KakiError("Skill引用文件不存在")
        rows, _ = self.catalog()
        for row in rows:
            directory = Path(row["directory"])
            if p.is_relative_to(directory) and p.relative_to(directory).as_posix() in row.get(
                "private_paths", []
            ):
                raise KakiError("此实现含受保护配置，不送入模型；通过已注册命令profile执行")
        if any(
            part.lower().startswith((".env", "credentials", "secrets"))
            or part.lower().endswith((".pem", ".key", ".p12"))
            for part in p.parts
        ):
            raise KakiError("Skill读取同样禁止凭据文件")
        return redact_text(p.read_text())


class Rules:
    # 读取工作区明确声明的规则文件，保留来源路径；规则加载不授予跨目录写权限。
    def __init__(self, workspace: Workspace):
        self.workspace = workspace

    def segment(self) -> str:
        # 把规则和实际来源路径放入提示，方便理解继承关系与定位约束。
        parts = []
        for path in self.workspace.rule_files:
            parts.append(f"## Repository instructions: {path}\n" + path.read_text(errors="replace"))
        return "\n\n".join(parts)
