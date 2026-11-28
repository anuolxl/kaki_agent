"""Typed Git/MR operations and administrator-configured command profiles."""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from kaki.config import redact_text
from kaki.execution import ExecutionTimeout, run_process
from kaki.permissions import PreparedOperation
from kaki.types import KakiError, ToolResult


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class DevelopmentTools:
    def __init__(self, runtime):
        self.runtime = runtime
        self.workspace, self.config = runtime.workspace, runtime.config

    def _env(self):
        if not self.config.tools.exec.path_append:
            return {}
        return {"PATH": os.environ.get("PATH", "") + os.pathsep + self.config.tools.exec.path_append}

    async def _git(self, *args, check=True):
        result = await run_process(
            ["git", "--no-pager", "-c", "core.fsmonitor=false", *args],
            self.workspace.root,
            60,
            {**self._env(), "GIT_OPTIONAL_LOCKS": "0"},
        )
        if check and result["exit_code"]:
            raise KakiError("Git失败：" + (result["stderr"] or result["stdout"])[:1200])
        return result

    async def state(self):
        head, branch, index, dirty = await asyncio.gather(
            self._git("rev-parse", "--verify", "HEAD", check=False),
            self._git("symbolic-ref", "--quiet", "--short", "HEAD", check=False),
            self._git("diff", "--cached", "--raw", "-z"),
            self._git("status", "--porcelain", "--untracked-files=no"),
        )
        return {
            "head": head["stdout"].strip() if not head["exit_code"] else "",
            "branch": branch["stdout"].strip() if not branch["exit_code"] else "",
            "index": digest(index["stdout"]),
            "dirty": dirty["stdout"],
        }

    def protected(self, branch):
        return not branch or any(
            fnmatch.fnmatchcase(branch, pattern)
            for pattern in self.config.tools.development.protected_branches
        )

    async def writable_state(self):
        state = await self.state()
        if self.protected(state["branch"]):
            raise KakiError("当前为保护分支或detached HEAD；请先通过kaki_git创建/切换开发分支")
        return state

    @staticmethod
    def ref(value):
        if (
            not value
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,180}", value)
            or any(p in value for p in ("..", "//", "@{"))
            or value.endswith(("/", ".", ".lock"))
        ):
            raise KakiError("Git引用格式不合法")
        return value

    def paths(self, values):
        if not values or len(values) > 100:
            raise KakiError("必须列出1–100个精确文件路径，禁止add .或批量纳入整个目录")
        result = []
        for value in values:
            path = self.workspace.path(value, write=True)
            if (
                path == self.workspace.root
                or path.is_dir()
                or any(
                    x in {".git", ".kaki", ".agents", ".codex"}
                    for x in path.relative_to(self.workspace.root).parts
                )
            ):
                raise KakiError("暂存/提交只接受明确业务文件，禁止目录和Agent控制目录")
            result.append(str(path.relative_to(self.workspace.root)))
        return sorted(set(result))

    async def remote(self, name):
        if name != "origin":
            raise KakiError("当前开发工具只使用已配置的origin，不接受模型新增远端")
        url = (await self._git("remote", "get-url", name))["stdout"].strip()
        if re.search(r"https?://[^/]*@|sk-[A-Za-z0-9_-]+", url):
            raise KakiError("远端URL不能包含内嵌凭据")
        return url

    async def prepare_git(self, a, ctx):
        cfg = self.config.tools.development
        if not cfg.enabled:
            raise KakiError("tools.development.enabled=false")
        action = a["action"]
        state = await self.state()
        resource = str(self.workspace.root)
        if action == "status":
            argv, cap = ["status", "--short", "--branch"], "workspace.read"
        elif action == "diff":
            argv, cap = (
                [
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    *(["--cached"] if a.get("staged") else []),
                    "--stat",
                ],
                "workspace.read",
            )
        elif action == "log":
            argv, cap = ["log", "-10", "--format=%h %s"], "workspace.read"
        elif action == "fetch":
            remote = a.get("remote", "origin")
            url = await self.remote(remote)
            ref = self.ref(a.get("ref", ""))
            argv, cap = (
                ["fetch", "--no-tags", remote, f"refs/heads/{ref}:refs/remotes/{remote}/{ref}"],
                "git.fetch",
            )
            resource += " origin=" + url
        elif action in {"create_branch", "switch"}:
            branch = self.ref(a.get("branch", ""))
            if self.protected(branch):
                raise KakiError("不能把保护分支作为开发分支")
            if state["dirty"]:
                raise KakiError("存在已跟踪文件改动，未stash/reset；请保护现场后再切分支")
            if action == "create_branch":
                base = self.ref(a.get("base", ""))
                await self._git("rev-parse", "--verify", base + "^{commit}")
                argv = ["switch", "-c", branch, base]
            else:
                argv = ["switch", branch]
            cap = "git.branch"
        elif action in {"stage", "commit"}:
            if self.protected(state["branch"]):
                raise KakiError("不能在保护分支暂存或提交")
            paths = self.paths(a.get("paths"))
            staged = sorted(
                p
                for p in (await self._git("diff", "--cached", "--name-only", "-z"))["stdout"].split("\0")
                if p
            )
            if set(staged) - set(paths):
                raise KakiError("Git index含本次明确文件之外的改动；未覆盖用户暂存区")
            if action == "stage":
                selected = {}
                for value in paths:
                    p = self.workspace.path(value, write=True)
                    if p.exists() and p.stat().st_size > 8 * 1024 * 1024:
                        raise KakiError("暂存文件超过8 MiB")
                    selected[value] = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "missing"
                state["selected_files"] = selected
                argv, cap = ["add", "--", *paths], "git.stage"
            else:
                message = a.get("message", "").strip()
                if staged != paths or not message or "\0" in message or len(message) > 8000:
                    raise KakiError("提交必须提供message，且paths必须与当前精确staged文件相同")
                argv, cap = ["commit", "-m", message], "git.commit"
        elif action == "push":
            if not cfg.allow_push or self.protected(state["branch"]):
                raise KakiError("推送未启用，或当前为保护分支")
            url = await self.remote(a.get("remote", "origin"))
            argv, cap = (
                ["push", "--set-upstream", "origin", f"HEAD:refs/heads/{self.ref(state['branch'])}"],
                "git.push",
            )
            resource += " origin=" + url
        else:
            raise KakiError("不支持此Git操作")
        return PreparedOperation(
            "kaki_git",
            cap,
            resource,
            digest(a),
            tuple(["git", "--no-pager", "-c", "core.fsmonitor=false", *argv]),
            state_token=digest(state),
        )

    async def git(self, a, ctx):
        operation = ctx["prepared_operation"]
        result = await run_process(
            operation.argv, self.workspace.root, 180, {**self._env(), "GIT_OPTIONAL_LOCKS": "0"}
        )
        data = {**result, "action": a["action"], "state": await self.state()}
        return ToolResult(
            json.dumps(data, ensure_ascii=False),
            failed=result["exit_code"] != 0,
            exit_code=result["exit_code"],
            context_refresh=result["exit_code"] == 0 and a["action"] in {"switch", "create_branch"},
        )

    def expand(self, text):
        return text.replace("{workspace}", str(self.workspace.root)).replace(
            "{state}", str(self.workspace.state)
        )

    def command(self, name):
        profile = self.config.tools.commands.get(name)
        if not profile:
            raise KakiError("未配置此命令profile；先用kaki_capabilities查看实际能力")
        return profile

    def command_catalog(self):
        rows = []
        for name, profile in self.config.tools.commands.items():
            executable = self.expand(profile.argv[0])
            valid = True
            for path, expected in profile.files.items():
                try:
                    valid = (
                        valid and hashlib.sha256(Path(self.expand(path)).read_bytes()).hexdigest() == expected
                    )
                except OSError:
                    valid = False
            rows.append(
                {
                    "name": name,
                    "description": profile.description,
                    "capability": profile.capability,
                    "timeout": profile.timeout,
                    "allowed_flags": profile.allowed_flags,
                    "path_flags": profile.path_flags,
                    "write_flags": profile.write_flags,
                    "available": bool(shutil.which(executable)) and valid,
                    "integrity_valid": valid,
                }
            )
        return rows

    def available_commands(self):
        return {row["name"] for row in self.command_catalog() if row["available"]}

    def command_path(self, value):
        path = Path(self.expand(value)).expanduser()
        return (path if path.is_absolute() else self.workspace.root / path).resolve()

    def command_arguments(self, profile, values):
        if values and not profile.allow_arguments:
            raise KakiError("此profile不接受附加参数")
        if len(values) > 80 or any("\0" in value or len(value) > 12000 for value in values):
            raise KakiError("附加参数超出限制")
        writes = []
        i = 0
        while i < len(values):
            arg = values[i]
            if arg.startswith("-"):
                flag = arg.split("=", 1)[0]
                if flag not in profile.allowed_flags:
                    raise KakiError("profile不允许该选项：" + flag)
                if flag in profile.path_flags or flag in profile.write_flags:
                    if "=" in arg or i + 1 >= len(values):
                        raise KakiError("文件选项必须使用独立的路径参数")
                    p = self.command_path(values[i + 1])
                    if flag in profile.write_flags:
                        if not p.is_relative_to(self.workspace.state / "workflows"):
                            raise KakiError("workflow输出只能写入当前项目state/workflows")
                        writes.append(p)
                    elif not (
                        p.is_relative_to(self.workspace.state / "artifacts")
                        or p.is_relative_to(self.workspace.state / "workflows")
                        or p.is_relative_to(self.workspace.root)
                    ):
                        raise KakiError("workflow输入不在当前项目或产物目录")
                    i += 1
            i += 1
        return writes

    async def prepare_command(self, a, ctx):
        profile = self.command(a["name"])
        values = a.get("arguments", [])
        self.command_arguments(profile, values)
        checked = {}
        review = {}
        for i, flag in enumerate(values[:-1]):
            if flag in profile.path_flags:
                p = self.command_path(values[i + 1])
                if p.stat().st_size > 8 * 1024 * 1024:
                    raise KakiError("workflow输入超过8 MiB")
                raw = p.read_bytes()
                checked[str(p)] = hashlib.sha256(raw).hexdigest()
                if flag == "--request-file":
                    try:
                        payload = json.loads(raw)
                        fields = payload.get("inputs", {})
                        review = {
                            "harnessTaskID": payload.get("harnessTaskID"),
                            "spaceNodeID": payload.get("spaceNodeID"),
                            "inputs": {
                                k: fields.get(k)
                                for k in (
                                    "env",
                                    "unit",
                                    "xTtEnv",
                                    "needDelploy",
                                    "needDeploy",
                                    "need_deploy",
                                    "ptd",
                                    "prd",
                                    "caseTosUrl",
                                )
                                if k in fields
                            },
                        }
                    except (ValueError, AttributeError):
                        review = {"input_file": str(p), "bytes": len(raw), "sha256": checked[str(p)]}
        for value, expected in profile.files.items():
            p = Path(self.expand(value)).expanduser().resolve()
            actual = await asyncio.to_thread(lambda p=p: hashlib.sha256(p.read_bytes()).hexdigest())
            if actual != expected:
                raise KakiError("可信profile依赖文件hash已变化，需由用户重新核对配置：" + str(p))
            checked[str(p)] = actual
        suffix = list(values)
        for i, flag in enumerate(values[:-1]):
            if flag in profile.path_flags or flag in profile.write_flags:
                suffix[i + 1] = str(self.command_path(values[i + 1]))
        argv = [self.expand(v) for v in profile.argv + suffix]
        if not shutil.which(argv[0]):
            raise KakiError("缺少profile执行程序：" + argv[0])
        cwd = Path(self.expand(profile.cwd or "{workspace}")).expanduser().resolve()
        if not cwd.is_dir():
            raise KakiError("profile工作目录不存在")
        return PreparedOperation(
            "kaki_command",
            profile.capability,
            str(cwd),
            digest(a),
            tuple(argv),
            state_token=digest(checked),
            review=review,
        )

    async def run_command(self, a, ctx):
        profile = self.command(a["name"])
        operation = ctx["prepared_operation"]
        for p in self.command_arguments(profile, a.get("arguments", [])):
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = await run_process(
            operation.argv, Path(operation.resource), profile.timeout, {**self._env(), **profile.env}
        )
        return ToolResult(
            json.dumps(result, ensure_ascii=False),
            failed=result["exit_code"] != 0,
            exit_code=result["exit_code"],
        )

    async def repository(self):
        url = await self.remote("origin")
        match = re.fullmatch(
            r"(?:git@|https://)(github\.com|code\.byted\.org|code-tx\.byted\.org)[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?",
            url,
        )
        if not match:
            raise KakiError("MR工具只支持GitHub和Codebase的origin")
        return match.group(1), match.group(2)

    async def prepare_mr(self, a, ctx):
        if not self.config.tools.development.enabled:
            raise KakiError("开发工具未启用")
        state = await self.writable_state()
        host, repo = await self.repository()
        base = self.ref(a.get("base", ""))
        action = a["action"]
        if action == "create_draft" and not self.config.tools.development.allow_mr_create:
            raise KakiError("草稿MR创建未启用；可以先使用prepare")
        if action not in {"prepare", "find", "create_draft"}:
            raise KakiError("MR工具不提供合并/部署操作")
        if action != "find" and (not a.get("title", "").strip() or not a.get("body", "").strip()):
            raise KakiError("准备/创建MR必须提供title与body")
        if any(redact_text(a.get(k, "")) != a.get(k, "") for k in ("title", "body")):
            raise KakiError("MR内容含凭据样式，拒绝保存或发送")
        return PreparedOperation(
            "kaki_mr",
            "repository.mr_create"
            if action == "create_draft"
            else "external.read"
            if action == "find"
            else "session.write",
            f"{host}/{repo} base={base} head={state['branch']}",
            digest(a),
            ("gh" if host == "github.com" else "codebase", "mr", action, base, state["branch"]),
            state_token=digest(state),
        )

    async def _find_mr(self, host, repo, head, base):
        if host == "github.com":
            argv = [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--head",
                head,
                "--base",
                base,
                "--state",
                "open",
                "--json",
                "number,url,isDraft,headRefName,baseRefName",
            ]
        else:
            argv = ["codebase", "mr", "list", "-R", repo, "--status", "open", "--page-size", "100"]
        result = await run_process(argv, self.workspace.root, 60, self._env())
        if result["exit_code"]:
            raise KakiError("MR查询失败：" + result["stderr"][:1000])
        try:
            data = json.loads(result["stdout"])
        except ValueError:
            raise KakiError("MR查询没有返回完整JSON") from None
        if host == "github.com":
            return data[0] if data else None
        rows = data.get("MergeRequests", [])
        return next(
            (r for r in rows if r.get("SourceBranchName") == head and r.get("TargetBranchName") == base), None
        )

    async def mr(self, a, ctx):
        state = await self.writable_state()
        host, repo = await self.repository()
        base = a["base"]
        if a["action"] == "prepare":
            artifact = self.workspace.artifact(a["body"].encode(), suffix=".md")
            return ToolResult(
                json.dumps(
                    {
                        "action": "prepare",
                        "repository": f"{host}/{repo}",
                        "head": state["branch"],
                        "base": base,
                        "source_commit": state["head"],
                        "title": a["title"],
                        "body_artifact": artifact,
                        "draft": True,
                        "created_remotely": False,
                    },
                    ensure_ascii=False,
                ),
                artifacts=[artifact],
            )
        existing = await self._find_mr(host, repo, state["branch"], base)
        if existing or a["action"] == "find":
            return ToolResult(
                json.dumps({"existing_mr": existing, "created_remotely": False}, ensure_ascii=False)
            )
        receipt = (
            self.workspace.state
            / "workflows"
            / "mr"
            / (digest([host, repo, state["branch"], base]) + ".json")
        )
        receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return ToolResult(
                "已有创建回执但查询尚未确认结果；不要重复创建，使用find或人工核对。receipt=" + str(receipt),
                failed=True,
                uncertain=True,
            )
        with os.fdopen(fd, "w") as out:
            json.dump(
                {
                    "state": "pending",
                    "repository": f"{host}/{repo}",
                    "head": state["branch"],
                    "base": base,
                    "source_commit": state["head"],
                },
                out,
            )
        if host == "github.com":
            artifact = self.workspace.artifact(a["body"].encode(), suffix=".md")
            argv = [
                "gh",
                "pr",
                "create",
                "--repo",
                repo,
                "--head",
                state["branch"],
                "--base",
                base,
                "--draft",
                "--title",
                a["title"],
                "--body-file",
                artifact["path"],
            ]
        else:
            argv = [
                "codebase",
                "mr",
                "create",
                "-R",
                repo,
                "--source",
                state["branch"],
                "--target",
                base,
                "--draft",
                "--title",
                a["title"],
                "--body",
                a["body"],
            ]
        try:
            result = await run_process(argv, self.workspace.root, 120, self._env())
        except (ExecutionTimeout, KakiError):
            return ToolResult(
                "MR创建结果未知，已保留回执；请只查询，不自动重试。receipt=" + str(receipt),
                failed=True,
                uncertain=True,
            )
        if result["exit_code"]:
            return ToolResult(
                json.dumps(
                    {**result, "receipt": str(receipt), "result": "unknown; query before retry"},
                    ensure_ascii=False,
                ),
                failed=True,
                uncertain=True,
            )
        receipt.write_text(json.dumps({"state": "created", "result": result}, ensure_ascii=False))
        return ToolResult(json.dumps({**result, "receipt": str(receipt)}, ensure_ascii=False))
