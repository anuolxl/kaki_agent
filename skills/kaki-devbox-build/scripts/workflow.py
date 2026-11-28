"""Bounded SSH adapter for exact-commit source transfer and background Devbox builds."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shlex
import uuid
from pathlib import Path

from remote_worker import DEPENDENCIES

from kaki.execution import run_process

MARKER = "KAKI_DEVBOX_JSON:"
BOOTSTRAP = r"""
import base64,hashlib,json,os,pathlib,sys
source=base64.b64decode(sys.argv[1]);request=json.loads(sys.argv[2]);digest=hashlib.sha256(source).hexdigest()
root=pathlib.Path.home()/'.kaki-devbox'/'runners';root.mkdir(parents=True,exist_ok=True)
runner=root/(digest+'.py')
if not runner.exists():runner.write_bytes(source);runner.chmod(0o600)
scope={'__name__':'kaki_remote'}
exec(compile(source,str(runner),'exec'),scope)
try:result=scope['dispatch'](request,runner);print('KAKI_DEVBOX_JSON:'+json.dumps({'ok':True,'result':result}))
except Exception as e:print('KAKI_DEVBOX_JSON:'+json.dumps({'ok':False,'error':str(e)[:1800]}));sys.exit(1)
"""


def parser():
    # 模型只能选择固定动作、仓库、提交和job；没有自由主机、Shell命令或口令参数。
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--action", choices=["prepare", "dependencies", "start", "status"], required=True)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--target", help="trusted profile prefix; must match KAKI_DEVBOX_TARGET")
    p.add_argument("--repo", choices=["sdk", "media"])
    p.add_argument("--branch")
    p.add_argument("--commit")
    p.add_argument("--task", required=True)
    p.add_argument("--mode", choices=["sdk-build", "sdk-test", "media-build", "media-test"])
    p.add_argument("--job")
    p.add_argument("--sdk-commit")
    p.add_argument("--wait", type=int, default=0)
    p.add_argument("--lines", type=int, default=40)
    p.add_argument("--timeout", type=int, default=1800)
    return p


async def main(args):
    # 本机协调器：校验目标 → 固定SSH适配 → 传精确源码或查询后台任务 → 保存私密回执。
    import base64

    target = os.environ.get("KAKI_DEVBOX_TARGET", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+", target):
        raise ValueError("trusted KAKI_DEVBOX_TARGET is required")
    if args.target and args.target != target:
        raise ValueError("target differs from trusted profile configuration")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", args.task):
        raise ValueError("invalid task identifier")
    if args.action in {"start", "status"} and not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", args.job or ""
    ):
        raise ValueError("start/status require a valid job ID")
    if args.action == "start" and (
        not args.repo or not args.mode or not args.mode.startswith(args.repo + "-")
    ):
        raise ValueError("start requires a matching repo/mode")
    if args.action == "start" and not 30 <= args.timeout <= 7200:
        raise ValueError("build timeout must be 30..7200 seconds")
    source = (Path(__file__).parent / "remote_worker.py").read_bytes()
    env = {"SSH_AUTH_SOCK": os.environ["SSH_AUTH_SOCK"]} if os.environ.get("SSH_AUTH_SOCK") else {}
    options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]

    async def remote(request):
        # 固定受信worker以内容hash命名送到用户专属缓存；未知响应先查同一job，不重发start。
        inner = shlex.join(
            ["python3", "-c", BOOTSTRAP, base64.b64encode(source).decode(), json.dumps(request)]
        )
        result = await run_process(
            ["ssh", "-T", *options, target, shlex.join(["bash", "-lc", inner])], Path.cwd(), 660, env
        )
        line = next(
            (line[len(MARKER) :] for line in result["stdout"].splitlines() if line.startswith(MARKER)), None
        )
        if line is None:
            raise RuntimeError(
                "SSH response unavailable; query the same job before retrying a start: "
                + result["stderr"][-1000:]
            )
        payload = json.loads(line)
        if not payload["ok"]:
            raise RuntimeError(payload["error"])
        return payload["result"]

    request = {
        key: value
        for key, value in vars(args).items()
        if key not in {"state", "target"} and value is not None
    }
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.action == "dependencies":
        origin = await run_process(["git", "remote", "get-url", "origin"], Path.cwd(), 15)
        if not re.search(r"[:/]larkvc/byteview-mp-cpp-sdk(?:\.git)?$", origin["stdout"].strip()):
            raise ValueError("prepare SDK dependencies from the matching SDK workspace")
        build = (Path.cwd() / "BUILD").read_text()
        results = {}
        git_env = {
            **env,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=2",
        }
        for name, dependency in sorted(DEPENDENCIES.items()):
            declaration = build
            if dependency.get("parent"):
                parent = dependency["parent"]
                viewed = await run_process(
                    ["git", "show", DEPENDENCIES[parent]["ref"] + ":BUILD"],
                    args.state / ("dependency-" + parent + ".git"),
                    15,
                )
                if viewed["exit_code"]:
                    raise ValueError("fixed parent dependency BUILD is unavailable")
                declaration = viewed["stdout"]
            if not re.search(
                re.escape(dependency["repo"] + ":" + dependency["ref"]) + r"(?=@|[\"'])", declaration
            ):
                raise ValueError("SDK BUILD dependency differs from the fixed workflow: " + name)
            cache = args.state / ("dependency-" + name + ".git")
            if not cache.exists():
                temporary = cache.with_name(cache.name + "." + uuid.uuid4().hex + ".tmp")
                cloned = await run_process(
                    [
                        "git",
                        "clone",
                        "--bare",
                        "--single-branch",
                        "--branch",
                        dependency["ref"],
                        "git@code.byted.org:" + dependency["repo"],
                        str(temporary),
                    ],
                    Path.cwd(),
                    180,
                    git_env,
                )
                if cloned["exit_code"]:
                    raise RuntimeError("local dependency access failed: " + cloned["stderr"][-1500:])
                temporary.rename(cache)
            revision = await run_process(["git", "rev-parse", dependency["ref"] + "^{commit}"], cache, 15)
            commit = revision["stdout"].strip()
            if revision["exit_code"] or not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise RuntimeError("cached dependency has no expected ref: " + name)
            bundle = args.state / ("dependency-" + name + "-" + commit + ".bundle")
            if not bundle.exists():
                packed = await run_process(["git", "bundle", "create", str(bundle), "--all"], cache, 180)
                if packed["exit_code"]:
                    raise RuntimeError(packed["stderr"][-1500:])
                bundle.chmod(0o600)
            checksum = hashlib.sha256()
            with bundle.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    checksum.update(chunk)
            item = {
                "task": args.task,
                "dependency": name,
                "commit": commit,
                "bundle_sha256": checksum.hexdigest(),
            }
            upload = await remote({**item, "action": "dependency-upload"})
            copied = await run_process(
                ["scp", "-q", *options, str(bundle), target + ":" + upload["path"]], Path.cwd(), 300, env
            )
            if copied["exit_code"]:
                raise RuntimeError(copied["stderr"][-1500:])
            results[name] = await remote({**item, "action": "dependency-prepare"})
        request = {"action": "dependencies", "task": args.task}
        result = results
    else:
        result = None
    if args.action == "prepare":
        if not args.repo or not re.fullmatch(r"[0-9a-f]{40}", args.commit or ""):
            raise ValueError("prepare requires repo and exact 40-character commit")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,180}", args.branch or "") or ".." in args.branch:
            raise ValueError("prepare requires a valid pushed source branch")
        expected = "byteview-mp-cpp-sdk" if args.repo == "sdk" else "byteview-mp-media_platform"
        origin = await run_process(["git", "remote", "get-url", "origin"], Path.cwd(), 15)
        if not re.search(r"[:/]larkvc/" + re.escape(expected) + r"(?:\.git)?$", origin["stdout"].strip()):
            raise ValueError("run prepare in the matching single-Git workspace")
        ref = "refs/heads/" + args.branch
        head = await run_process(["git", "rev-parse", "--verify", ref + "^{commit}"], Path.cwd(), 15)
        if head["exit_code"] or head["stdout"].strip() != args.commit:
            raise ValueError("source branch/commit mismatch; uncommitted edits are never transferred")
        bundle = args.state / (args.repo + "-" + args.commit + ".bundle")
        if not bundle.exists():
            temporary = bundle.with_name(bundle.name + "." + uuid.uuid4().hex + ".tmp")
            packed = await run_process(["git", "bundle", "create", str(temporary), ref], Path.cwd(), 300)
            if packed["exit_code"]:
                raise RuntimeError(packed["stderr"][-1500:])
            temporary.chmod(0o600)
            temporary.replace(bundle)
        checked = await run_process(["git", "bundle", "verify", str(bundle)], Path.cwd(), 30)
        if checked["exit_code"]:
            raise RuntimeError("cached bundle is invalid; retained for inspection")
        checksum = hashlib.sha256()
        with bundle.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(chunk)
        digest = checksum.hexdigest()
        upload = await remote({**request, "action": "upload"})
        copied = await run_process(
            ["scp", "-q", *options, str(bundle), target + ":" + upload["path"]], Path.cwd(), 600, env
        )
        if copied["exit_code"]:
            raise RuntimeError(copied["stderr"][-1500:])
        request["bundle_sha256"] = digest
    if result is None:
        result = await remote(request)
    receipt = args.state / (args.task + "-" + (args.job or args.repo or "status") + ".json")
    receipt.write_text(
        json.dumps({"target": target, "request": request, "result": result}, ensure_ascii=False, indent=2)
    )
    receipt.chmod(0o600)
    print(json.dumps({"target": target, "result": result, "receipt": str(receipt)}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(parser().parse_args()))
