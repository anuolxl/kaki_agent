"""Validated local capabilities; tool results are observations, never task verdicts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

from kaki.config import Config, safe_error
from kaki.types import InteractionRequired, KakiError, ToolCall, ToolResult
from kaki.workspace import Workspace

from .context.tokens import count_text, excerpt


@dataclass
class Tool:
    name: str
    description: str
    properties: dict
    required: list[str]
    effect: str
    execute: Callable[[dict, dict], Awaitable[ToolResult]]
    safe_read: bool = False
    schema: dict | None = None

    def parameters(self):
        return self.schema or {
            "type": "object",
            "properties": self.properties,
            "required": self.required,
            "additionalProperties": False,
        }

    def definition(self):
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
    def __init__(self, workspace: Workspace, config: Config, context, browser, ask, permissions=None):
        self.workspace, self.config, self.context, self.browser, self.ask = (
            workspace,
            config,
            context,
            browser,
            ask,
        )
        self.tools = {}
        self.permissions = permissions
        self._builtins()

    def add(self, tool: Tool):
        if tool.name not in self.config.tools.disabled_tools:
            self.tools[tool.name] = tool

    def _builtins(self):
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
                "Run one supported argv command without a shell: pwd/echo, git status/diff/ls-files, pytest/python -m unittest/go test/node --test/npm test/cargo test/make test. No pipes, redirects, scripts or arbitrary commands. Test execution requires user task authorization or approval.",
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
        for t in await self.browser.start():
            name = t["name"]

            async def call(args, ctx, n=name):
                return await self.browser.call(n, args)

            self.add(Tool(name, t["description"], {}, [], "EXTERNAL", call, schema=t["parameters"]))

    def definitions(self):
        from kaki.permissions import BLOCKED_BROWSER_TOOLS

        excluded = {"kaki_browser_" + suffix for suffix in BLOCKED_BROWSER_TOOLS}
        if self.config.permissions.mode == "read_only":
            excluded |= {"kaki_write_file", "kaki_edit_file", "kaki_remember"}
        return [t.definition() for t in self.tools.values() if t.name not in excluded]

    async def prepare(self, call, ctx):
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
            operation = self.permissions.prepare(call, args)
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
        prepared = prepared or await self.prepare(call, ctx)
        call, operation, decision = prepared
        if isinstance(decision, ToolResult):
            return decision
        tool = self.tools[call.name]
        args = call.arguments
        entered = False
        try:
            # INVARIANT: recheck canonical targets after human approval and before actual I/O.
            if self.permissions.prepare(call, args) != operation:
                return ToolResult(
                    "授权后操作目标已改变，请重新读取", failed=True, denied=True, executed=False
                )
            if operation.capability in {"workspace.write", "test.execute"} and ctx.get("before_mutation"):
                await ctx["before_mutation"]()
            if self.permissions.prepare(call, args) != operation:
                return ToolResult("快照期间目标已改变，未执行", failed=True, denied=True, executed=False)
            if on_start:
                await on_start(call)
            entered = True
            if call.name == "kaki_ask_user":
                result = await tool.execute(args, ctx)
            else:
                timeout = (
                    (args.get("timeout") or self.config.tools.exec.timeout) + 10
                    if tool.effect == "EXECUTE"
                    else 120
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
        except Exception as exc:
            return ToolResult(
                "工具失败：" + safe_error(exc) + "\n请分析真实错误并更换方法，不要重复无效调用。",
                failed=True,
                executed=entered,
                decision_id=decision,
            )

    def bound(self, result: ToolResult, name: str) -> ToolResult:
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
                    prepared = [await self.prepare(c, ctx) for c in batch]
                    async with asyncio.TaskGroup() as group:
                        tasks = [group.create_task(one(c, p)) for c, p in zip(batch, prepared, strict=True)]
                    results.extend(task.result() for task in tasks)
                i = end
            else:
                results.append(await one(calls[i], await self.prepare(calls[i], ctx)))
                i += 1
        return results

    async def read(self, a, ctx):
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
        p = self.workspace.path(a.get("path", "."))
        entries = await asyncio.to_thread(lambda: sorted(p.iterdir()))
        return ToolResult(
            "\n".join(x.name + ("/" if x.is_dir() else "") for x in entries[:500] if x.name != ".git")
        )

    async def search(self, a, ctx):
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
        p = self.workspace.path(a["path"], write=True)
        if a.get("expected_hash") and (
            not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != a["expected_hash"]
        ):
            raise KakiError("文件hash已变化，拒绝覆盖；请重新读取")
        await self._atomic_write(p, a["content"].encode())
        return ToolResult("已写入 " + str(p), touched_paths=[str(p)])

    async def edit(self, a, ctx):
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
        answer = await self.ask(a["question"], a.get("options", []))
        # A business clarification is evidence, not approval, and invalidates stale code-search hints.
        if ctx.get("request"):
            ctx["request"].rewrite.queries["code"] = [ctx["user_text"]]
        return ToolResult(answer)

    async def skill_read(self, a, ctx):
        return ToolResult(await asyncio.to_thread(self.context.skills.read, a["path"]))

    async def memory_search(self, a, ctx):
        return ToolResult(json.dumps(await self.context.memory.recall(a["query"]), ensure_ascii=False))

    async def remember(self, a, ctx):
        identity = await self.context.memory.remember(
            a["content"], [ctx["user_message_id"]], replaces=a.get("replaces")
        )
        return ToolResult("已保存项目记忆：" + identity)

    async def history_search(self, a, ctx):
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
        return ToolResult(
            json.dumps(
                await self.context.archive.read(
                    ctx["session_id"], a["archive_ref"], a.get("offset", 0), a.get("limit", 8000)
                ),
                ensure_ascii=False,
            )
        )

    async def artifact_read(self, a, ctx):
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
