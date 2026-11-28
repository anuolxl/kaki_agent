"""Install Kaki Skills and pinned local command profiles; never bundle private credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import yaml


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bundle_references(source, target):
    """Make trusted local Markdown dependencies self-contained without adding extra Skills."""
    mapping = {p.resolve(): target / p.relative_to(source) for p in source.rglob("*.md")}
    queue = list(mapping)
    total = 0
    for original in queue:
        if len(queue) > 100:
            raise ValueError("Skill reference closure exceeds 100 Markdown files")
        destination = mapping[original]
        text = destination.read_text()

        def replace(match):
            nonlocal total
            link, anchor = match.group(1), match.group(2) or ""
            if "://" in link or not link.endswith(".md"):
                return match.group(0)
            dependency = (original.parent / link).resolve()
            if not dependency.is_file() or not dependency.is_relative_to(source.parent):
                return match.group(0)
            if dependency not in mapping:
                total += dependency.stat().st_size
                if total > 8 * 1024 * 1024:
                    raise ValueError("Skill reference closure exceeds 8 MiB")
                name = dependency.relative_to(source.parent).as_posix().replace("/", "__")
                mapped = target / "references" / "upstream" / name
                mapped.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dependency, mapped)
                mapping[dependency] = mapped
                queue.append(dependency)
            relative = os.path.relpath(mapping[dependency], destination.parent)
            return "](" + relative + anchor + ")"

        destination.write_text(re.sub(r"\]\(([^)#]+)(#[^)]*)?\)", replace, text))


def install(args):
    home = args.home.expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = home / "config.json"
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    if config_path.exists():
        backup = home / f"config.before-development-{time.time_ns()}.json"
        backup.write_bytes(config_path.read_bytes())
        backup.chmod(0o600)
    skills = home / "skills"
    skills.mkdir(exist_ok=True, mode=0o700)
    source = Path(__file__).resolve().parents[1] / "skills"
    installed = []
    for name in ("kaki-code-development", "kaki-code-review", "kaki-prepare-mr"):
        target = skills / name
        target.mkdir(exist_ok=True, mode=0o700)
        shutil.copy2(source / name / "SKILL.md", target / "SKILL.md")
        installed.append(name)
    devbox = skills / "kaki-devbox-build"
    shutil.copytree(
        source / "kaki-devbox-build",
        devbox,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for path in devbox.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    installed.append("kaki-devbox-build")
    tools = config.setdefault("tools", {})
    development = tools.setdefault("development", {})
    development["enabled"] = True
    protected_key = (
        "protected_branches"
        if "protected_branches" in development and "protectedBranches" not in development
        else "protectedBranches"
    )
    development[protected_key] = list(
        dict.fromkeys(development.get(protected_key, []) + ["master", "main", "release/*"])
    )
    if "allowPush" not in development and "allow_push" not in development:
        development["allowPush"] = True
    if "allowMrCreate" not in development and "allow_mr_create" not in development:
        development["allowMrCreate"] = True
    commands = tools.setdefault("commands", {})
    harness = args.harness.resolve()
    doctor = harness / "omh/prechecks/omh-doctor.sh"
    common = doctor.parent / "omh-common.sh"
    if not doctor.is_file() or not common.is_file():
        raise ValueError("Harness freshness scripts not found")
    commands["freshness"] = dict(
        description="只读检查 Harness master freshness doctor，不升级、不切分支，不检查完整OMH环境",
        argv=["bash", str(doctor), "--repository-freshness"],
        cwd=str(harness),
        capability="workspace.check",
        timeout=120,
        files={str(p): sha(p) for p in (doctor, common)},
    )
    if args.devbox_target:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+", args.devbox_target):
            raise ValueError("--devbox-target must be user@host")
        interpreter = str(args.python.expanduser().absolute())
        source_scripts = devbox / "scripts"
        package_hash = hashlib.sha256(
            b"".join(
                path.name.encode() + b"\0" + path.read_bytes() for path in sorted(source_scripts.glob("*.py"))
            )
        ).hexdigest()
        scripts = home / "skill-runtimes" / "kaki-devbox-build" / package_hash
        scripts.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in sorted(source_scripts.glob("*.py")):
            destination = scripts / path.name
            if destination.exists():
                if destination.read_bytes() != path.read_bytes():
                    raise ValueError("immutable Skill runtime content mismatch")
            else:
                shutil.copy2(path, destination)
                destination.chmod(0o400)
        env = {"KAKI_DEVBOX_TARGET": args.devbox_target}
        if os.environ.get("SSH_AUTH_SOCK"):
            env["SSH_AUTH_SOCK"] = os.environ["SSH_AUTH_SOCK"]
        probe = scripts / "devbox_probe.py"
        commands["devbox-inspect"] = dict(
            description="登录已配置SSH Devbox读取系统、工具链和仓库元数据，不修改远端业务代码",
            argv=[interpreter, str(probe)],
            capability="external.read",
            timeout=100,
            env=env,
            files={str(probe): sha(probe)},
        )
        pins = {str(scripts / name): sha(scripts / name) for name in ["workflow.py", "remote_worker.py"]}
        for name, action, capability, timeout, flags in [
            (
                "devbox-dependencies",
                "dependencies",
                "external.write",
                1500,
                ["--task"],
            ),
            (
                "devbox-prepare",
                "prepare",
                "external.write",
                1500,
                ["--repo", "--branch", "--commit", "--task"],
            ),
            (
                "devbox-build",
                "start",
                "external.write",
                100,
                ["--repo", "--mode", "--task", "--job", "--sdk-commit", "--timeout"],
            ),
            ("devbox-status", "status", "external.read", 100, ["--task", "--job", "--wait", "--lines"]),
        ]:
            commands[name] = dict(
                description="Devbox隔离编译工作流 "
                + action
                + "；固定主机/仓库/模式，不接受任意Shell；启动不代表编译成功",
                argv=[
                    interpreter,
                    str(scripts / "workflow.py"),
                    "--target",
                    args.devbox_target,
                    "--state",
                    "{state}/workflows/devbox",
                    "--action",
                    action,
                ],
                capability=capability,
                timeout=timeout,
                env=env,
                files=pins,
                allowArguments=True,
                allowedFlags=flags,
            )
    if args.private_skill_source:
        original = args.private_skill_source.resolve()
        target = skills / "vc-casegen-task"
        meta_text = (original / "SKILL.md").read_text()
        _, front, body = meta_text.split("---", 2)
        meta = yaml.safe_load(front)
        if meta.get("name") != "vc-casegen-task":
            raise ValueError("private Skill source has wrong name")
        if target.exists():
            # Only replace this known, user-selected Skill, preserving a local backup.
            shutil.copytree(target, home / f"vc-casegen-task.backup-{time.time_ns()}")
        shutil.copytree(
            original, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        meta.setdefault("metadata", {})["kaki"] = {
            "required_tools": ["kaki_command", "kaki_artifact_write"],
            "required_commands": ["casegen-validate", "casegen-materials", "casegen-submit", "casegen-query"],
            "private_paths": ["scripts/task_api.py"],
        }
        adapter = """
## Kaki 执行入口（先读）

Kaki 不解释下文 Shell 变量；用 kaki_command 的命名 profile 执行同一官方脚本。
先用 kaki_artifact_write 保存本轮 source-request.json，返回 artifact.path 为真实路径。
以 {state}/workflows/<本轮ID>/ 保存 request.json、materials.json、submission.json、result.json。
profile 的参数是 JSON arguments 数组，禁止将整段 Shell 命令作为参数：

1. casegen-materials: --request-file <artifact.path> --output {state}/workflows/<ID>/request.json --receipt {state}/workflows/<ID>/materials.json --use-proxy
2. casegen-validate: --request-file {state}/workflows/<ID>/request.json
3. casegen-submit: --request-file {state}/workflows/<ID>/request.json --receipt {state}/workflows/<ID>/submission.json --output {state}/workflows/<ID>/result.json --use-proxy
4. casegen-query: --receipt {state}/workflows/<ID>/submission.json --output {state}/workflows/<ID>/result.json --use-proxy

上传和创建属于 external.write，必须检查用户实际授权和具体参数。未提供 env/unit/xTtEnv/triggerUser/PTD 等真实信息时只准备材料并询问缺失项，不虚构、不提交。
默认用 submit 返回回执后单次 query；需要等待时重复只读查询，或 query --wait --wait-timeout 300 --interval 15。
不读取 scripts/task_api.py，其中的受保护鉴权实现只由受校验的脚本执行。安装并不等于远端认证成功。
以下原始契约及其反重复提交、材料上传和 workflow 终态规则继续有效。
"""
        (target / "SKILL.md").write_text(
            "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n" + adapter + body
        )
        bundle_references(original, target)
        for p in target.rglob("*"):
            p.chmod(0o700 if p.is_dir() else 0o600)
        scripts = target / "scripts"
        pins = {str(p): sha(p) for p in sorted(scripts.glob("*.py"))}
        interpreter = str(args.python.expanduser().absolute())
        if not Path(interpreter).is_file():
            raise ValueError("configured Python executable does not exist")
        profiles = {
            "casegen-validate": (
                "test_task.py",
                "validate",
                "workspace.check",
                60,
                ["--request-file"],
                ["--request-file"],
                [],
            ),
            "casegen-materials": (
                "prepare_materials.py",
                None,
                "external.write",
                600,
                ["--request-file", "--output", "--receipt", "--use-proxy", "--timeout"],
                ["--request-file"],
                ["--output", "--receipt"],
            ),
            "casegen-submit": (
                "test_task.py",
                "submit",
                "external.write",
                180,
                ["--request-file", "--receipt", "--output", "--use-proxy", "--request-timeout"],
                ["--request-file"],
                ["--receipt", "--output"],
            ),
            "casegen-query": (
                "test_task.py",
                "query",
                "external.read",
                420,
                [
                    "--receipt",
                    "--harness-task-id",
                    "--space-node-id",
                    "--output",
                    "--use-proxy",
                    "--wait",
                    "--wait-timeout",
                    "--interval",
                    "--request-timeout",
                ],
                ["--receipt"],
                ["--output"],
            ),
        }
        for name, (script, action, capability, timeout, flags, reads, writes) in profiles.items():
            commands[name] = dict(
                description=f"VC中心化测试 {action or 'materials'} 官方脚本；遵守vc-casegen-task契约，保留回执，创建不重发",
                argv=[interpreter, str(scripts / script)] + ([action] if action else []),
                capability=capability,
                timeout=timeout,
                allowArguments=True,
                allowedFlags=flags,
                pathFlags=reads,
                writeFlags=writes,
                files=pins,
            )
        installed.append("vc-casegen-task")
    temp = home / f".config-{os.getpid()}.json"
    with temp.open("x") as out:
        json.dump(config, out, ensure_ascii=False, indent=2)
    temp.chmod(0o600)
    temp.replace(config_path)
    return {
        "skills": installed,
        "profiles": sorted(commands),
        "config": str(config_path),
        "note": "No remote task submitted. Restart existing Kaki processes to load changes.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("KAKI_HOME", "~/.kaki")))
    parser.add_argument("--harness", type=Path, required=True)
    parser.add_argument("--private-skill-source", type=Path)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--devbox-target", help="可选的已授权SSH目标user@host；不登录、不自动启动远端编译")
    print(json.dumps(install(parser.parse_args()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
