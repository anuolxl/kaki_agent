"""Application-level capability checks, not an OS sandbox for approved child processes."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from urllib.parse import urlparse

from kaki.decisions import APPROVAL_LABELS
from kaki.types import KakiError, ToolResult
from kaki.understanding.strategies import NO_TEST, PLAN_TEST, RUN_TEST

GLOBAL_NO_WRITE = re.compile(
    r"只(?:回答|读|分析|介绍|解释)|不(?:要|得|能)?(?:修改|改动|编辑|写入)(?:任何|本地|仓库)?(?:文件|代码|工作区)|read[ -]?only|do not (?:edit|modify|write)(?: files| code)?[.!]?$",
    re.I,
)
BLOCKED_BROWSER_TOOLS = {"run_code", "evaluate", "install", "file_upload"}


def edit_request(text):
    clauses = re.split(r"[，,。\n;]", text)
    for clause in clauses:
        if re.search(r"如何|怎么|是否|方案|计划|建议|不要|不修改|do not|how to|whether", clause, re.I):
            continue
        if re.search(
            r"^(?:请|你|帮我|直接|现在|完成|在\S+中|对\S+)(?:你|帮我|直接|现在|完成|在\S+中|对\S+|\s)*(?:修复|修改|实现|重构|新增|创建|写入|删除|替换)|^(?:修复|修改|实现|重构|新增|创建|写入|删除|替换)\s*|^(?:please\s+)?(?:fix|edit|implement|refactor|create|write|delete)\b",
            clause.strip(),
            re.I,
        ):
            return True
    return False


@dataclass(frozen=True)
class PreparedOperation:
    name: str
    capability: str
    resource: str
    fingerprint: str
    argv: tuple[str, ...] = ()
    file_state: str | None = None


def structured_command(command):
    # No shell grammar is accepted, even inside quotes. Quoting is solely argv grouping.
    if re.search(r"[\n\r;&|<>`$]", command):
        raise KakiError("只支持单条结构化命令，禁止管道、重定向、变量和命令替换")
    try:
        argv = shlex.split(command)
    except ValueError:
        raise KakiError("命令引号不完整") from None
    if not argv:
        raise KakiError("命令不能为空")
    if argv[0] == "uv" and argv[1:2] == ["run"]:
        argv = argv[2:]
    if not argv or "/" in argv[0] or "\\" in argv[0]:
        raise KakiError("命令必须使用支持的可执行程序名")
    name = argv[0]
    if name in {"pwd", "echo"}:
        if name == "pwd" and len(argv) != 1:
            raise KakiError("pwd不接受额外参数")
        return tuple(argv), "workspace.read"
    if name == "git":
        if len(argv) < 2 or argv[1] not in {"status", "diff", "ls-files"}:
            raise KakiError("Git仅开放status/diff/ls-files；历史内容请由用户在终端查看")
        if any(
            a.startswith(("--output", "--ext-diff", "--textconv", "--no-index", "--pathspec-from-file"))
            or a in {"-c", "-C", "--git-dir", "--work-tree"}
            for a in argv[2:]
        ):
            raise KakiError("不允许Git外部执行、输出重定向或切换仓库")
        # Disable hooks/pagers/configured external diff at the executable boundary.
        return tuple(
            [
                "git",
                "--no-pager",
                "-c",
                "core.fsmonitor=false",
                argv[1],
                *(["--no-ext-diff", "--no-textconv"] if argv[1] == "diff" else []),
                *argv[2:],
            ]
        ), "workspace.read"
    test = (
        name in {"pytest", "py.test"}
        or name in {"python", "python3"}
        and argv[1:3] in (["-m", "unittest"], ["-m", "pytest"])
        or name == "go"
        and argv[1:2] == ["test"]
        or name == "node"
        and argv[1:2] == ["--test"]
        or name == "npm"
        and (argv[1:2] == ["test"] or argv[1:3] == ["run", "test"])
        or name == "cargo"
        and argv[1:2] == ["test"]
        or name == "make"
        and len(argv) == 2
        and argv[1] in {"test", "docker-test"}
    )
    if test:
        if any(
            a.startswith(("--output", "--junitxml", "--basetemp", "--rootdir", "--confcutdir"))
            or a.startswith("/")
            or ".." in a.split("/")
            for a in argv[1:]
        ):
            raise KakiError("测试参数不能指定工作区外路径或重定向输出")
        return tuple(argv), "test.execute"
    raise KakiError("不支持此命令模板；请使用文件/搜索工具，或由用户在终端执行")


class PermissionService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.grants = set()
        self.denials = set()

    def begin(self):
        self.grants.clear()
        self.denials.clear()

    def prepare(self, call, args):
        rt = self.runtime
        name = call.name
        argv = ()
        file_state = None
        resource = str(rt.workspace.root)
        if name in {"kaki_read_file", "kaki_list_dir", "kaki_search", "kaki_write_file", "kaki_edit_file"}:
            writing = name in {"kaki_write_file", "kaki_edit_file"}
            capability = "workspace.write" if writing else "workspace.read"
            resource = str(rt.workspace.path(args.get("path", "."), write=writing))
            if writing and any(
                p in {".kaki", ".agents", ".codex", ".git", ".github"}
                for p in rt.workspace.path(args["path"]).relative_to(rt.workspace.root).parts
            ):
                raise KakiError("不允许工具修改Agent配置、Skills或Git/CI控制文件")
            if writing and rt.workspace.path(args["path"]).name in {"AGENTS.md", "KAKI.md", "CLAUDE.md"}:
                raise KakiError("仓库指令文件需由用户单独维护")
            if writing:
                path = rt.workspace.path(args["path"], write=True)
                if path.exists():
                    if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                        raise KakiError("写入目标必须是8 MiB以内的普通文件")
                    file_state = hashlib.sha256(path.read_bytes()).hexdigest()
                else:
                    file_state = "missing"
        elif name == "kaki_exec":
            argv, capability = structured_command(args["command"])
            resource = str(rt.workspace.path(args.get("working_dir", ".")))
            if not rt.workspace.path(resource).is_dir():
                raise KakiError("working_dir必须是目录")
            if argv[0] == "git":
                # Git diff obeys workspace credential exclusions, including historical versions.
                if "--" in argv:
                    split = argv.index("--")
                    for path in argv[split + 1 :]:
                        rt.workspace.path(path)
                    prefix, paths = argv[:split], argv[split + 1 :]
                else:
                    prefix, paths = argv, (".",)
                argv = (
                    *prefix,
                    "--",
                    *paths,
                    ":(exclude)**/.env*",
                    ":(exclude)**/*credentials*",
                    ":(exclude)**/*secrets*",
                    ":(exclude)**/*.pem",
                    ":(exclude)**/*.key",
                    ":(exclude)**/*.p12",
                    ":(exclude)**/.kaki/**",
                    ":(exclude)**/.agents/**",
                    ":(exclude)**/.codex/**",
                )
        elif name == "kaki_remember":
            capability = "memory.write"
        elif name == "kaki_ask_user":
            capability = "interaction.ask_user"
        elif name.startswith("kaki_browser_"):
            suffix = name.removeprefix("kaki_browser_")
            if suffix in BLOCKED_BROWSER_TOOLS:
                raise KakiError("浏览器任意代码执行、安装和本地文件上传未开放")
            if suffix == "navigate":
                parsed = urlparse(args.get("url", ""))
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username
                    or parsed.password
                ):
                    raise KakiError("浏览器仅允许无内嵌凭据的HTTP(S)地址")
                if re.search(r"sk-[A-Za-z0-9_-]{12,}", args["url"]):
                    raise KakiError("URL不得包含模型凭据")
                capability, resource = "browser.observe", parsed.netloc
            elif suffix in {
                "snapshot",
                "take_screenshot",
                "tabs",
                "close",
                "console_messages",
                "network_requests",
                "navigate_back",
                "resize",
                "wait_for",
            }:
                if suffix == "tabs" and args.get("action", "list") != "list":
                    capability = "browser.interact"
                else:
                    capability = "browser.observe"
            else:
                capability = "browser.interact"
                if rt.config.permissions.mcp.unknown_tool == "deny":
                    raise KakiError("此浏览器工具未被显式允许")
        elif name in {
            "kaki_skill_read",
            "kaki_history_search",
            "kaki_archive_read",
            "kaki_memory_search",
            "kaki_artifact_read",
        }:
            capability = "session.read"
            if name == "kaki_skill_read":
                # Validate the configured skill root before granting a read.
                from pathlib import Path

                path = Path(args["path"]).expanduser().resolve()
                if not any(path.is_relative_to(root.resolve()) for root in rt.context.skills.roots):
                    raise KakiError("不在本地Skill根目录")
        else:
            raise KakiError("未登记能力的工具不能执行")
        fingerprint = hashlib.sha256(
            json.dumps({"name": name, "args": args}, sort_keys=True).encode()
        ).hexdigest()
        return PreparedOperation(name, capability, resource, fingerprint, argv, file_state)

    def task_allows(self, operation, human):
        mode = self.runtime.config.permissions.mode
        cap = operation.capability
        if cap in {"workspace.read", "session.read", "interaction.ask_user", "browser.observe"}:
            return True
        if mode in {"read_only", "ask"}:
            return False
        if cap == "workspace.write":
            if not edit_request(human) or GLOBAL_NO_WRITE.search(human):
                return False
            paths = re.findall(
                r"[A-Za-z0-9_./-]+\.(?:py|go|js|ts|json|md|yaml|yml|toml|txt)(?![A-Za-z0-9_])", human
            )
            if paths:
                target = self.runtime.workspace.path(operation.resource)
                return any(self.runtime.workspace.path(p, write=True) == target for p in paths)
            return True
        if cap == "test.execute":
            return bool(RUN_TEST.search(human) and not NO_TEST.search(human) and not PLAN_TEST.search(human))
        if cap == "memory.write":
            return bool(
                re.search(r"记住|remember|保存.*记忆", human, re.I)
                and not re.search(r"不.*记住|do not remember", human, re.I)
            )
        return False

    def restriction(self, operation, human):
        if operation.capability == "workspace.write" and GLOBAL_NO_WRITE.search(human):
            return "当前用户明确限制文件写入"
        if operation.capability == "test.execute" and NO_TEST.search(human):
            return "当前用户明确禁止运行测试"
        if operation.capability == "workspace.write":
            # Conservatively honor path-specific prohibitions instead of guessing their scope.
            forbidden = re.findall(r"(?:不(?:要)?(?:修改|改动)|不要动)\s*`?([\w./-]+)", human)
            relative = str(
                self.runtime.workspace.path(operation.resource).relative_to(self.runtime.workspace.root)
            )
            if re.search(r"不(?:要)?(?:修改|改动).*测试|do not (?:modify|edit).*tests?", human, re.I) and (
                any(part in {"test", "tests"} for part in relative.split("/"))
                or relative.split("/")[-1].startswith("test_")
                or relative.endswith("_test.go")
            ):
                return "用户要求保留测试文件"
            if any(relative == p or relative.startswith(p.rstrip("/") + "/") for p in forbidden):
                return "目标路径被用户排除"
        return None

    async def authorize(self, operation, ctx, arguments=None):
        rt = self.runtime
        human = ctx.get("human_text", ctx.get("user_text", ""))
        restriction = self.restriction(operation, human)
        scope = (
            operation.argv
            if operation.capability == "test.execute"
            else operation.fingerprint
            if operation.capability == "browser.interact"
            else ""
        )
        key = (operation.capability, operation.resource, scope)
        if restriction:
            return ToolResult("权限拒绝：" + restriction, failed=True, executed=False, denied=True)
        if operation.fingerprint in self.denials:
            return ToolResult(
                "权限拒绝：本轮已拒绝该操作，不重复询问", failed=True, executed=False, denied=True
            )
        cap = operation.capability
        if rt.config.permissions.mode == "read_only" and cap not in {
            "workspace.read",
            "session.read",
            "interaction.ask_user",
            "browser.observe",
        }:
            return ToolResult("权限拒绝：read_only配置未开放此能力", failed=True, executed=False, denied=True)
        if key in self.grants or self.task_allows(operation, human):
            return None
        if rt.config.permissions.default_decision == "deny":
            return ToolResult("权限拒绝：本地配置默认拒绝", failed=True, executed=False, denied=True)
        options = [APPROVAL_LABELS[s] for s in rt.config.permissions.grant_scopes] + [APPROVAL_LABELS["deny"]]
        details = {
            "workspace": str(rt.workspace.root),
            "session_id": rt.session_id,
            "turn_id": rt.active_turn,
            "tool": operation.name,
            "capability": cap,
            "resource": operation.resource,
            "argv": list(operation.argv),
            "fingerprint": operation.fingerprint,
            "file_state": operation.file_state,
            "arguments_preview": {
                key: value[:2000] if isinstance(value, str) else value
                for key, value in (arguments or {}).items()
            },
        }
        question = "需要授权后才能执行：\n" + json.dumps(details, ensure_ascii=False, indent=2)
        if cap == "test.execute":
            question += "\n测试会运行仓库代码；当前没有操作系统沙箱，脚本可能写文件或访问网络。"
        answer = await rt.decisions.request("approval", question, options, details)
        if answer["choice"] == "deny":
            self.denials.add(operation.fingerprint)
            return ToolResult(
                "用户拒绝执行；未启动工具",
                failed=True,
                executed=False,
                denied=True,
                decision_id=answer["decision_id"],
            )
        if answer["choice"] == "turn":
            self.grants.add(key)
        return answer["decision_id"]
