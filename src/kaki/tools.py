"""Validated local capabilities; tool results are observations, never task verdicts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from jsonschema import Draft202012Validator

from kaki.config import Config, redact_text, safe_error
from kaki.development import DevelopmentTools, digest
from kaki.execution import ExecutionTimeout
from kaki.types import InteractionRequired, KakiError, ToolCall, ToolResult
from kaki.workspace import Workspace

from .context.tokens import count_text, excerpt


@dataclass
class Tool:
    # 受信工具描述：给模型的 Schema 与真正 Python 执行器并存，模型不能自行声明执行权限。
    # 模型可见的稳定工具名。
    name: str
    # 会进入模型提示的能力说明，普通代码注释不能改变它。
    description: str
    # 本地函数参数定义。
    properties: dict
    # 必须由模型提供的字段；不等于操作已获准。
    required: list[str]
    # 调度效应分类；写操作不会混进并发安全读批次。
    effect: str
    # 受信异步实现，由宿主注册而非模型传入。
    execute: Callable[[dict, dict], Awaitable[ToolResult]]
    # 只有明确安全的 READ 才可批量并发。
    safe_read: bool = False
    # MCP 等完整外部 Schema，仍需本地执行前验证。
    schema: dict | None = None
    # 能力发现分类，不是权限等级。
    category: str = "core"
    # 本轮发现/Skill 激活后才把 Schema 放进模型窗口。
    deferred: bool = False
    # 受信预检回调，把参数转成可复核操作。
    preflight: Callable | None = None

    def parameters(self):
        # 本地工具用字段构造 Schema，MCP 工具保留服务端完整 Schema；二者都在执行前验证。
        return self.schema or {
            "type": "object",
            "properties": self.properties,
            "required": self.required,
            "additionalProperties": False,
        }

    def definition(self):
        # 生成 Responses function 描述；可见不代表该用户任务已经授权。
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters(),
            "strict": False,
        }


def string(description=""):
    return {"type": "string", "description": description}


def integer(description="", minimum=0, maximum=10000):
    return {"type": "integer", "description": description, "minimum": minimum, "maximum": maximum}


class ToolRegistry:
    # 统一能力入口：注册/发现、参数校验、权限、快照前置、执行和输出限额都在这里汇合。
    def __init__(self, workspace: Workspace, config: Config, context, browser, ask, permissions=None):
        # 绑定当前工作区与内核服务；没有 PermissionService 时采用拒绝执行的边界。
        self.workspace, self.config, self.context, self.browser, self.ask = (
            workspace,
            config,
            context,
            browser,
            ask,
        )
        self.tools = {}
        # 缺少权限服务时采用拒绝执行，而非默认全能力。
        self.permissions = permissions
        self.development = DevelopmentTools(permissions.runtime) if permissions else None
        # 本轮激活的延迟 Schema 名字；每 Turn 清空，工具注册本身仍保留。
        self.selected_tools = set()
        self._builtins()
        self._development_tools()

    def add(self, tool: Tool):
        # 本机 disabled_tools 是能力上限，Skill 或模型不能绕过它重新注册。
        if tool.name not in self.config.tools.disabled_tools:
            self.tools[tool.name] = tool

    def _development_tools(self):
        # 仅按可信配置注册 Git、MR 和命名命令；描述里提到能力不会安装可执行程序。
        self.add(
            Tool(
                "kaki_capabilities",
                "Inspect actual tools, command profiles and local Skills. Includes prerequisites; user consent cannot install a missing tool.",
                {},
                [],
                "READ",
                self.capabilities,
                True,
            )
        )
        self.add(
            Tool(
                "kaki_tool_search",
                "Discover and load relevant tools or command profiles by task query. 查找Git分支、MR、测试平台、浏览器等能力；返回的schema不授予权限。",
                {"query": string(), "limit": integer("results", 1, 10)},
                ["query"],
                "READ",
                self.tool_search,
                True,
            )
        )
        self.add(
            Tool(
                "kaki_artifact_write",
                "Save bounded text/JSON as an immutable project artifact outside the Git worktree; use its path for workflow input or MR material.",
                {"text": string(), "format": {"type": "string", "enum": ["text", "json", "markdown"]}},
                ["text"],
                "READ",
                self.artifact_write,
            )
        )
        self.add(
            Tool(
                "kaki_skill_search",
                "Search installed local Skills by task; return exact names, source paths, prerequisites and readiness. 先检索合适Skill，再skill_read读取正文和必要reference，不自动执行。",
                {"query": string(), "limit": integer("results", 1, 10)},
                ["query"],
                "READ",
                self.skill_search,
                True,
            )
        )
        if not self.development:
            return
        if self.config.tools.commands:
            self.add(
                Tool(
                    "kaki_command",
                    "Execute a named, user-configured command profile with integrity checks. 用于freshness doctor、开发检查和测试平台脚本；先查看capabilities，不接受任意bash字符串。",
                    {"name": string(), "arguments": {"type": "array", "items": string(), "maxItems": 80}},
                    ["name"],
                    "EXECUTE",
                    self.development.run_command,
                    category="development",
                    preflight=self.development.prepare_command,
                )
            )
        if self.config.tools.development.enabled:
            self.add(
                Tool(
                    "kaki_git",
                    "Typed local Git development: status/diff/log/fetch/create_branch/switch/stage/commit/push. 更新release基线、新建开发分支、精确暂存和提交。No reset/clean/force/protected-branch writes. stage/commit require exact paths; create_branch requires base and branch. Fetch uses ref=release/... .",
                    {
                        "action": {
                            "type": "string",
                            "enum": [
                                "status",
                                "diff",
                                "log",
                                "fetch",
                                "create_branch",
                                "switch",
                                "stage",
                                "commit",
                                "push",
                            ],
                        },
                        "remote": string(),
                        "ref": string(),
                        "base": string(),
                        "branch": string(),
                        "paths": {"type": "array", "items": string(), "minItems": 1, "maxItems": 100},
                        "message": string(),
                        "staged": {"type": "boolean"},
                    },
                    ["action"],
                    "EXECUTE",
                    self.development.git,
                    category="development",
                    preflight=self.development.prepare_git,
                )
            )
            self.add(
                Tool(
                    "kaki_mr",
                    "Prepare a reviewable MR draft artifact, find an existing MR, or create an approved draft MR. Existing MR reuse does not edit title/body. 支持GitHub/Codebase，复用不等于更新；远端创建需单次授权，不合并、不部署。",
                    {
                        "action": {"type": "string", "enum": ["prepare", "find", "create_draft"]},
                        "base": string(),
                        "title": string(),
                        "body": string(),
                    },
                    ["action", "base"],
                    "EXTERNAL",
                    self.development.mr,
                    category="repository",
                    deferred=True,
                    preflight=self.development.prepare_mr,
                )
            )

    def begin_turn(self):
        # 清除上轮延迟 Schema 激活状态，避免会话越跑工具定义越多。
        self.selected_tools.clear()

    def activate(self, names):
        # 发现或读取 Skill 后只激活已有工具；不从模型文本动态导入代码。
        self.selected_tools.update(name for name in names if name in self.tools)

    def available_names(self):
        # 报告注册且未被配置隐藏的名字，当前操作是否获准还要经过 authorize。
        return set(self.tools) - self.excluded()

    def _builtins(self):
        # 核心本地能力表；效应分类和 safe_read 决定调度顺序，不交给模型自由填写。
        definitions = [
            Tool(
                "kaki_read_file",
                "Read UTF-8 file with line numbers, SHA256 and pagination. Read before editing.",
                {"path": string(), "offset": integer("1-based line", 1), "limit": integer("lines", 1, 2000)},
                ["path"],
                "READ",
                self.read,
                True,
            ),
            Tool(
                "kaki_list_dir",
                "List a workspace directory without accessing Git internals.",
                {"path": string()},
                [],
                "READ",
                self.list_dir,
                True,
            ),
            Tool(
                "kaki_search",
                "Search literal text or regex inside workspace files; returns paths and line numbers.",
                {
                    "query": string(),
                    "path": string(),
                    "glob": string(),
                    "regex": {"type": "boolean"},
                    "use_task_queries": {
                        "type": "boolean",
                        "description": "Opt in to one task-derived literal search hint, within the same path/glob. Default false.",
                    },
                },
                ["query"],
                "READ",
                self.search,
                True,
            ),
            Tool(
                "kaki_write_file",
                "Create/write a UTF-8 file. Pass expected_hash to protect against concurrent edits.",
                {"path": string(), "content": string(), "expected_hash": string()},
                ["path", "content"],
                "WRITE",
                self.write,
            ),
            Tool(
                "kaki_edit_file",
                "Replace unique old_text with new_text; ambiguous anchors fail. Read the file first.",
                {
                    "path": string(),
                    "old_text": string(),
                    "new_text": string(),
                    "replace_all": {"type": "boolean"},
                    "expected_hash": string(),
                },
                ["path", "old_text", "new_text"],
                "WRITE",
                self.edit,
            ),
            Tool(
                "kaki_exec",
                "Run structured commands: pwd/echo, read-only git, test runners, make test ARGS=..., or gofmt -w <one.go>. For Git writes use kaki_git; for bash/workflow scripts use configured kaki_command profiles. No arbitrary shell string/pipes/redirects.",
                {"command": string(), "working_dir": string(), "timeout": integer("seconds", 1, 600)},
                ["command"],
                "EXECUTE",
                self.exec,
            ),
            Tool(
                "kaki_ask_user",
                "Ask the user a required clarification and wait; do not guess missing requirements.",
                {"question": string(), "options": {"type": "array", "items": string()}},
                ["question"],
                "EXTERNAL",
                self.ask_user,
            ),
            Tool(
                "kaki_skill_read",
                "Read a configured local Skill or its bounded local Markdown reference.",
                {"path": string()},
                ["path"],
                "READ",
                self.skill_read,
                True,
            ),
            Tool(
                "kaki_history_search",
                "Find previous conversation evidence and archive references.",
                {"query": string()},
                ["query"],
                "READ",
                self.history_search,
                True,
            ),
            Tool(
                "kaki_archive_read",
                "Read an exact paginated lossless history archive, using an opaque archive_ref.",
                {"archive_ref": string(), "offset": integer(), "limit": integer("characters", 100, 16000)},
                ["archive_ref"],
                "READ",
                self.archive_read,
                True,
            ),
        ]
        if self.config.memory.backend is not None:
            definitions += [
                Tool(
                    "kaki_memory_search",
                    "Recall active project memory with sources.",
                    {"query": string()},
                    ["query"],
                    "READ",
                    self.memory_search,
                    True,
                ),
                Tool(
                    "kaki_remember",
                    "Remember a reusable fact explicitly requested by the user; never save secrets or guesses.",
                    {"content": string(), "replaces": string()},
                    ["content"],
                    "WRITE",
                    self.remember,
                ),
            ]
        for t in definitions:
            self.add(t)

    async def connect_browser(self):
        # 读取 MCP list_tools 并映射名称；浏览器能力仍要经过本地逐工具权限门。
        for t in await self.browser.start():
            name = t["name"]

            async def call(args, ctx, n=name):
                return await self.browser.call(n, args)

            self.add(
                Tool(
                    name,
                    t["description"],
                    {},
                    [],
                    "EXTERNAL",
                    call,
                    schema=t["parameters"],
                    category="browser",
                    deferred=True,
                )
            )

    def definitions(self):
        # 仅把常驻/本轮激活的 Schema 放入模型窗口，避免一次注入整个 MCP 工具库。
        excluded = self.excluded()
        return [
            t.definition()
            for t in self.tools.values()
            if t.name not in excluded
            and (not self.config.tools.discovery.enabled or not t.deferred or t.name in self.selected_tools)
        ]

    def excluded(self):
        # 固定高风险浏览器能力及 read_only 文件写工具不展示；执行时仍有权限校验兜底。
        from kaki.permissions import BLOCKED_BROWSER_TOOLS

        excluded = {"kaki_browser_" + suffix for suffix in BLOCKED_BROWSER_TOOLS}
        if self.config.permissions.mode == "read_only":
            excluded |= {"kaki_write_file", "kaki_edit_file", "kaki_remember"}
        return excluded

    async def operation(self, tool, call, args, ctx):
        # 生成宿主规范化操作；模型参数只提供数据，不能伪造 capability 或文件状态。
        operation = (
            await tool.preflight(args, ctx) if tool.preflight else self.permissions.prepare(call, args)
        )
        if operation.capability == "workspace.write" and self.config.tools.development.enabled:
            state = await self.development.writable_state()
            operation = replace(operation, state_token=digest(state))
        return operation

    async def prepare(self, call, ctx):
        # Schema、路径与能力预检串行进行；需要授权时先等明确选择，再返回固定操作。
        tool = self.tools.get(call.name)
        if not tool:
            return None, None, ToolResult("工具不存在：" + call.name, failed=True, executed=False)
        args = dict(call.arguments)
        try:
            for key, prop in tool.parameters().get("properties", {}).items():
                if prop.get("type") == "integer" and isinstance(args.get(key), str) and args[key].isdigit():
                    args[key] = int(args[key])
            errors = sorted(
                Draft202012Validator(tool.parameters()).iter_errors(args), key=lambda e: str(e.path)
            )
            if errors:
                return (
                    None,
                    None,
                    ToolResult(
                        "参数校验失败：" + "; ".join(e.message for e in errors)[:1200],
                        failed=True,
                        executed=False,
                    ),
                )
            if self.permissions is None:
                return (
                    None,
                    None,
                    ToolResult("未初始化权限服务，拒绝执行", failed=True, denied=True, executed=False),
                )
            if call.name == "kaki_ask_user" and not self.permissions.runtime.interactive:
                raise InteractionRequired("clarification", args["question"])
            operation = await self.operation(tool, call, args, ctx)
            # Schema与路径通过并不等于获准；必要时保持 planned，等待唯一交互服务。
            answer = await self.permissions.authorize(operation, ctx, args)
            if isinstance(answer, ToolResult):
                return None, None, answer
            return ToolCall(call.id, call.name, args), operation, answer
        except InteractionRequired:
            raise
        except KakiError as exc:
            return (
                None,
                None,
                ToolResult("权限或路径拒绝：" + safe_error(exc), failed=True, denied=True, executed=False),
            )

        except (ValueError, OSError) as exc:
            return None, None, ToolResult("操作预检失败：" + safe_error(exc), failed=True, executed=False)

    async def execute(self, call: ToolCall, ctx: dict, *, on_start=None, prepared=None) -> ToolResult:
        # 批准后及快照后各复核一次目标；记录 running 后才启动真正 I/O。
        prepared = prepared or await self.prepare(call, ctx)
        call, operation, decision = prepared
        if isinstance(decision, ToolResult):
            return decision
        tool = self.tools[call.name]
        args = call.arguments
        entered = False
        try:
            # INVARIANT: recheck canonical targets after human approval and before actual I/O.
            # 审批等待期间目标可能改变；以宿主重新计算的操作描述复核，而非相信旧页面。
            if await self.operation(tool, call, args, ctx) != operation:
                return ToolResult(
                    "授权后操作目标已改变，请重新读取", failed=True, denied=True, executed=False
                )
            # 只有实际获准的文件写入/测试才请求基线快照，拒绝的工具不会触发全仓快照。
            if operation.capability in {"workspace.write", "test.execute"} and ctx.get("before_mutation"):
                await ctx["before_mutation"]()
            if await self.operation(tool, call, args, ctx) != operation:
                return ToolResult("快照期间目标已改变，未执行", failed=True, denied=True, executed=False)
            # 这一回调先写 running，再进入真实实现；异常恢复才知道动作是否已经开始。
            if on_start:
                await on_start(call)
            entered = True
            if call.name == "kaki_ask_user":
                result = await tool.execute(args, ctx)
            else:
                timeout = (
                    self.config.tools.commands[args["name"]].timeout + 10
                    if call.name == "kaki_command"
                    else 240
                    if call.name == "kaki_mr"
                    else 190
                    if call.name == "kaki_git"
                    else (
                        (args.get("timeout") or self.config.tools.exec.timeout) + 10
                        if tool.effect == "EXECUTE"
                        else 120
                    )
                )
                result = await asyncio.wait_for(
                    tool.execute(args, {**ctx, "prepared_operation": operation}), timeout
                )
            result.decision_id = decision
            return self.bound(result, call.name)
        except asyncio.CancelledError:
            raise
        except InteractionRequired:
            raise
        except ExecutionTimeout as exc:
            return ToolResult(
                safe_error(exc),
                failed=True,
                executed=entered,
                uncertain=entered
                and operation.capability
                not in {"workspace.read", "session.read", "external.read", "workspace.check"},
            )
        except Exception as exc:
            return ToolResult(
                "工具失败：" + safe_error(exc) + "\n请分析真实错误并更换方法，不要重复无效调用。",
                failed=True,
                executed=entered,
                decision_id=decision,
            )

    def bound(self, result: ToolResult, name: str) -> ToolResult:
        # 对观察脱敏并限制模型可见长度；完整结果保存在受控 artifact 中按需分页读取。
        result.text = redact_text(result.text)
        if count_text(result.text) > 4000:
            artifact = self.workspace.artifact(result.text.encode(), suffix=".txt")
            result.artifacts.append(artifact)
            result.text = (
                excerpt(result.text, 3000)
                + "\n... [输出有截断]\n尾部：\n"
                + excerpt(result.text[-5000:], 700)
                + f"\n完整输出artifact_id={artifact['artifact_id']}"
            )
        return result

    async def execute_many(self, calls: list[ToolCall], ctx: dict, on_start, on_complete) -> list[ToolResult]:
        # 仅连续 safe READ 最多四并发；其余串行，分支变化后整批余项先不执行。
        async def one(c, prepared):
            result = await self.execute(c, ctx, on_start=on_start, prepared=prepared)
            await on_complete(c, result)
            return result

        results = []
        i = 0
        while i < len(calls):
            t = self.tools.get(calls[i].name)
            if t and t.effect == "READ" and t.safe_read:
                end = i
                while end < len(calls):
                    s = self.tools.get(calls[end].name)
                    if not s or s.effect != "READ" or not s.safe_read:
                        break
                    end += 1
                for at in range(i, end, 4):
                    batch = calls[at : min(at + 4, end)]
                    # Preflight serially so concurrent reads never create concurrent human decisions.
                    # 预检/人类审批串行，避免并发读同时弹出多个问题。
                    prepared = [await self.prepare(c, ctx) for c in batch]
                    async with asyncio.TaskGroup() as group:
                        tasks = [group.create_task(one(c, p)) for c, p in zip(batch, prepared, strict=True)]
                    results.extend(task.result() for task in tasks)
                i = end
            else:
                result = await one(calls[i], await self.prepare(calls[i], ctx))
                results.append(result)
                i += 1
                # 成功切分支会使同批后续操作的依据过期，必须让模型在新规则下重新提出。
                if result.context_refresh:
                    for call in calls[i:]:
                        skipped = ToolResult(
                            "分支已变化；需要刷新规则后重新提出工具调用，本项未执行",
                            failed=True,
                            executed=False,
                        )
                        await on_complete(call, skipped)
                        results.append(skipped)
                    break
        return results

    async def read(self, a, ctx):
        # 返回带稳定行号、文件 hash 和下一页位置的原文，供精确编辑而非让模型猜测内容。
        p = self.workspace.path(a["path"])
        if p.stat().st_size > 8 * 1024 * 1024:
            raise KakiError("文件超过8 MiB读取上限，请用搜索或Shell按范围查看")
        raw = await asyncio.to_thread(p.read_bytes)
        if b"\0" in raw[:8000]:
            raise KakiError("二进制文件，请使用适合该格式的工具")
        text = raw.decode("utf-8")
        lines = text.splitlines()
        offset = a.get("offset", 1)
        limit = a.get("limit", 200)
        body = "\n".join(
            f"{i + 1}: {line}" for i, line in enumerate(lines) if offset - 1 <= i < offset - 1 + limit
        )
        return ToolResult(
            json.dumps(
                {
                    "path": str(p),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "total_lines": len(lines),
                    "next_offset": offset + limit if offset - 1 + limit < len(lines) else None,
                    "text": body,
                },
                ensure_ascii=False,
            )
        )

    async def list_dir(self, a, ctx):
        # 列目录只描述当前工作树，Git 元数据和跨仓路径仍由 Workspace 拒绝。
        p = self.workspace.path(a.get("path", "."))
        entries = await asyncio.to_thread(lambda: sorted(p.iterdir()))
        return ToolResult(
            "\n".join(x.name + ("/" if x.is_dir() else "") for x in entries[:500] if x.name != ".git")
        )

    async def search(self, a, ctx):
        # 原始搜索词优先；可选任务派生词只增加受限检索，不替换显式路径/正则。
        path = self.workspace.path(a.get("path", "."))
        args = [
            "rg",
            "--line-number",
            "--with-filename",
            "--max-count",
            "50",
            "--glob",
            "!.git/**",
            "--glob",
            "!.env*",
            "--glob",
            "!**/*.{pem,key,p12}",
            "--glob",
            "!**/{credentials,secrets}*",
            "--glob",
            "!**/{.kaki,.agents,.codex}/**",
        ]
        if not a.get("regex", False):
            args += ["--fixed-strings"]
        if a.get("glob"):
            args += ["--glob", a["glob"]]
        queries = [a["query"]]
        request = ctx.get("request")
        if a.get("use_task_queries") and not a.get("regex") and request:
            queries += [q for q in request.queries_for("code")[1:] if q != a["query"]][:1]
        results = []
        failed = False
        for query in queries:
            result = await self._search_once([*args, "--", query, str(path)])
            failed = failed or result.failed
            results.extend(result.text.splitlines())
        return ToolResult("\n".join(dict.fromkeys(results)) or "No matches found", failed=failed)

    async def _search_once(self, args):
        # 执行一次有范围和结果上限的本地搜索，不把找到的内容当新指令。
        try:
            process = await asyncio.create_subprocess_exec(
                *args, cwd=self.workspace.root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except FileNotFoundError:
            raise KakiError("缺少rg，请安装ripgrep以使用搜索") from None
        try:
            out, err = await asyncio.wait_for(process.communicate(), 20)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        return ToolResult(
            (out or err).decode(errors="replace") or "No matches found",
            failed=process.returncode not in (0, 1),
        )

    async def _atomic_write(self, p: Path, raw: bytes):
        # 在同目录写临时文件再 replace，避免取消留下半个源文件。
        def operation():
            p.parent.mkdir(parents=True, exist_ok=True)
            mode = p.stat().st_mode & 0o777 if p.exists() else 0o644
            temp = p.parent / (".kaki-write-" + str(os.getpid()) + "-" + str(time.time_ns()))
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(raw)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp, p)
            finally:
                if temp.exists():
                    temp.unlink()

        await asyncio.to_thread(operation)

    async def write(self, a, ctx):
        # 写文件前使用规范路径和可选预期版本；权限与保护分支检查已在公共入口完成。
        p = self.workspace.path(a["path"], write=True)
        if a.get("expected_hash") and (
            not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != a["expected_hash"]
        ):
            raise KakiError("文件hash已变化，拒绝覆盖；请重新读取")
        await self._atomic_write(p, a["content"].encode())
        return ToolResult("已写入 " + str(p), touched_paths=[str(p)])

    async def edit(self, a, ctx):
        # 按精确原文锚点替换；hash 不符、锚点缺失或不唯一都拒绝，避免覆盖并发修改。
        p = self.workspace.path(a["path"], write=True)
        raw = await asyncio.to_thread(p.read_bytes)
        if a.get("expected_hash") and hashlib.sha256(raw).hexdigest() != a["expected_hash"]:
            raise KakiError("文件hash已变化，拒绝编辑")
        crlf = b"\r\n" in raw
        text = raw.decode().replace("\r\n", "\n")
        old = a["old_text"].replace("\r\n", "\n")
        new = a["new_text"].replace("\r\n", "\n")
        if not old:
            raise KakiError("old_text不能为空")
        count = text.count(old)
        if not count:
            raise KakiError("未找到old_text，请重新读取文件")
        if count > 1 and not a.get("replace_all"):
            raise KakiError(f"old_text出现{count}次，锚点不唯一；请提供更多上下文")
        result = text.replace(old, new) if a.get("replace_all") else text.replace(old, new, 1)
        if crlf:
            result = result.replace("\n", "\r\n")
        await self._atomic_write(p, result.encode())
        return ToolResult("已修改 " + str(p), touched_paths=[str(p)])

    async def exec(self, a, ctx):
        # 只执行 structured_command 已解析的 argv，保留 uv runner；不把字符串交给任意 Shell。
        command = a["command"]
        cwd = self.workspace.path(a.get("working_dir", "."))
        operation = ctx.get("prepared_operation")
        if operation is None:
            raise KakiError("命令必须先通过权限预检")
        argv = operation.argv
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            in {
                "PATH",
                "HOME",
                "SHELL",
                "TMPDIR",
                "LANG",
                "LC_ALL",
                "USER",
                "LOGNAME",
                "TERM",
                "GOPATH",
                "GOROOT",
                "GOPROXY",
                "GOPRIVATE",
                "GONOSUMDB",
                "GONOPROXY",
                "CGO_ENABLED",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "NO_PROXY",
            }
        }
        if self.config.tools.exec.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.config.tools.exec.path_append
        if argv[0] == "git":
            env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        async def capture(reader):
            chunks = []
            size = 0
            discarded = 0
            while chunk := await reader.read(65536):
                room = max(0, 8 * 1024 * 1024 - size)
                chunks.append(chunk[:room])
                size += min(room, len(chunk))
                discarded += max(0, len(chunk) - room)
            return b"".join(chunks), discarded

        async def collect():
            stdout, stderr, _ = await asyncio.gather(
                capture(process.stdout), capture(process.stderr), process.wait()
            )
            return stdout, stderr

        try:
            (out, out_discarded), (err, err_discarded) = await asyncio.wait_for(
                collect(), a.get("timeout", self.config.tools.exec.timeout)
            )
        except BaseException:
            if process.returncode is None:
                with __import__("contextlib").suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except TimeoutError:
                    with __import__("contextlib").suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            raise
        payload = {
            "command": command,
            "argv": list(argv),
            "cwd": str(cwd),
            "exit_code": process.returncode,
            "stdout": out.decode(errors="replace"),
            "stderr": err.decode(errors="replace"),
            "capture_discarded_bytes": {"stdout": out_discarded, "stderr": err_discarded},
        }
        return ToolResult(
            json.dumps(payload, ensure_ascii=False),
            failed=process.returncode != 0,
            exit_code=process.returncode,
        )

    async def ask_user(self, a, ctx):
        # 把交互委托 Runtime；普通补充回答不自动等于权限批准。
        answer = await self.ask(a["question"], a.get("options", []))
        # A business clarification is evidence, not approval, and invalidates stale code-search hints.
        if ctx.get("request"):
            ctx["request"].rewrite.queries["code"] = [ctx["user_text"]]
        return ToolResult(answer)

    async def skill_read(self, a, ctx):
        # 按需读 Skill 引用并激活其依赖工具；私有实现文件仍受 Skills.read 限制。
        rows, _ = await asyncio.to_thread(self.context.skills.catalog)
        for row in rows:
            if Path(a["path"]).expanduser().resolve().is_relative_to(Path(row["directory"])):
                self.activate(row.get("required_tools", []))
        return ToolResult(await asyncio.to_thread(self.context.skills.read, a["path"]))

    async def capabilities(self, a, ctx):
        # 把已注册工具、profile 完整性和 Skill 缺项分开报告；不凭程序存在声称认证成功。
        visible = {row["name"] for row in self.definitions()}
        rows, failures = await asyncio.to_thread(self.context.skills.catalog)
        commands = await asyncio.to_thread(self.development.command_catalog) if self.development else []
        return ToolResult(
            json.dumps(
                {
                    "tools": [
                        {
                            "name": t.name,
                            "description": t.description[:500],
                            "category": t.category,
                            "visible": t.name in visible,
                            "deferred": t.deferred,
                        }
                        for t in self.tools.values()
                        if t.name in self.available_names()
                    ],
                    "commands": commands,
                    "skills": [
                        {
                            "id": r["id"],
                            "ready": r["ready"],
                            "missing_tools": r["missing_tools"],
                            "missing_commands": r["missing_commands"],
                        }
                        for r in rows
                    ],
                    "skill_failures": failures,
                    "development": self.config.tools.development.model_dump(),
                    "note": "注册/发现不等于授权；认证以实际CLI结果为准。",
                },
                ensure_ascii=False,
            )
        )

    async def tool_search(self, a, ctx):
        # 检索工具/profile 元数据后加载少量 Schema；检索命中仍不能越过执行权限。
        from kaki.understanding.retrieval import fused_rank

        entries = [
            {"name": t.name, "description": t.description, "tool": t.name}
            for t in self.tools.values()
            if t.name in self.available_names()
        ]
        entries += [
            {
                "name": name,
                "description": c.description,
                "tool": "kaki_command",
                "profile": name,
                "capability": c.capability,
                "allowed_flags": c.allowed_flags,
                "path_flags": c.path_flags,
                "write_flags": c.write_flags,
            }
            for name, c in self.config.tools.commands.items()
        ]
        query = a["query"]
        ranked = fused_rank([query], [e["name"] + " " + e["description"] for e in entries])
        explicit = [i for i, e in enumerate(entries) if e["name"] == query]
        indices = list(dict.fromkeys([*explicit, *(i for i, _ in ranked)]))[
            : min(
                a.get("limit", self.config.tools.discovery.max_results),
                self.config.tools.discovery.max_results,
            )
        ]
        hits = [entries[i] for i in indices]
        self.activate([e["tool"] for e in hits])
        return ToolResult(
            json.dumps(
                {
                    "matches": hits,
                    "loaded_definitions": [
                        self.tools[name].definition()
                        for name in dict.fromkeys(e["tool"] for e in hits)
                        if name in self.tools
                    ],
                },
                ensure_ascii=False,
            )
        )

    async def artifact_write(self, a, ctx):
        # 把工作流输入写到项目 state，而不是用户 Git 工作树；数据不可携带凭据样式。
        if redact_text(a["text"]) != a["text"]:
            raise KakiError("输入产物包含凭据样式，不保存或转交模型/外部平台")
        raw = a["text"].encode()
        if len(raw) > 2 * 1024 * 1024:
            raise KakiError("单个输入产物超过2 MiB")
        fmt = a.get("format", "text")
        if fmt == "json":
            json.loads(a["text"])
        artifact = self.workspace.artifact(
            raw, suffix={"json": ".json", "markdown": ".md", "text": ".txt"}[fmt]
        )
        return ToolResult(json.dumps(artifact, ensure_ascii=False), artifacts=[artifact])

    async def memory_search(self, a, ctx):
        return ToolResult(json.dumps(await self.context.memory.recall(a["query"]), ensure_ascii=False))

    async def remember(self, a, ctx):
        # 长期记忆必须有真实来源并通过显式记忆权限，不自动保存模型臆测。
        identity = await self.context.memory.remember(
            a["content"], [ctx["user_message_id"]], replaces=a.get("replaces")
        )
        return ToolResult("已保存项目记忆：" + identity)

    async def history_search(self, a, ctx):
        # 回查完整 Session 原文，用稳定消息 ID 对照压缩后的线索。
        from .understanding.retrieval import fused_rank

        messages = await ctx["store"].run("messages", ctx["session_id"])
        state = await ctx["store"].run("context_get", ctx["session_id"])
        hits = [
            {
                "message_id": messages[i].id,
                "role": messages[i].role,
                "snippet": excerpt(messages[i].content, 300),
                "archive_ref": state["manifest"].get(str(messages[i].id), {}).get("archive_ref"),
            }
            for i, _ in fused_rank([a["query"]], [m.content for m in messages])[:10]
        ]
        return ToolResult(json.dumps(hits, ensure_ascii=False))

    async def archive_read(self, a, ctx):
        # 归档只在所属会话内按范围读取，不能把任意文件路径当引用。
        return ToolResult(
            json.dumps(
                await self.context.archive.read(
                    ctx["session_id"], a["archive_ref"], a.get("offset", 0), a.get("limit", 8000)
                ),
                ensure_ascii=False,
            )
        )

    async def artifact_read(self, a, ctx):
        # 通过受控 artifact_id 读取完整工具输出，避免为截断结果重复执行副作用。
        p = self.workspace.artifact_path(a["artifact_id"])
        raw = await asyncio.to_thread(p.read_bytes)
        offset = a.get("offset", 0)
        limit = a.get("limit", 8000)
        if b"\0" in raw[:8000]:
            raise KakiError("该附件是二进制内容，不能当文本读取")
        text = raw.decode("utf-8")
        return ToolResult(
            json.dumps(
                {
                    "text": text[offset : offset + limit],
                    "offset": offset,
                    "next_offset": offset + limit if offset + limit < len(text) else None,
                },
                ensure_ascii=False,
            )
        )

    async def skill_search(self, a, ctx):
        # 检索全体已安装 Skill 元数据与就绪情况，不受首页 catalog 数量限制。
        from kaki.understanding.retrieval import fused_rank

        rows, failures = await asyncio.to_thread(self.context.skills.catalog)
        hits = fused_rank(
            [a["query"]], [r["id"] + " " + r["description"] + " " + r["body"][:1800] for r in rows]
        )
        limit = min(a.get("limit", 5), 10)
        return ToolResult(
            json.dumps(
                {
                    "skills": [
                        {
                            k: rows[i][k]
                            for k in (
                                "id",
                                "path",
                                "description",
                                "required_tools",
                                "required_commands",
                                "ready",
                                "missing_tools",
                                "missing_commands",
                            )
                        }
                        for i, _ in hits[:limit]
                    ],
                    "failures": failures,
                },
                ensure_ascii=False,
            )
        )
