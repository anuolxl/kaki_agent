"""Prepare declared Go module archives without transferring credentials or compiling locally."""

import hashlib
import json
import re
import shutil
import tarfile
import uuid
from pathlib import Path

from kaki.execution import run_process


def escape_module(value):
    if not re.fullmatch(r"[A-Za-z0-9_.+!~/-]+", value) or ".." in value or value.startswith("/"):
        raise ValueError("invalid module path/version")
    return "".join("!" + char.lower() if char.isupper() else char for char in value)


def declared_modules(text):
    # go.mod的require含直接/间接版本；go.sum另外提供历史图元数据，不猜latest。
    result = set()
    for line in text.splitlines():
        line = re.sub(r"^\s*require\s+", "", line)
        match = re.match(r"\s*([A-Za-z0-9_.+!~/-]+)\s+(v[0-9][A-Za-z0-9.+-]*)(?:\s|$)", line)
        if match:
            result.add(match.groups())
    return result


def metadata_modules(text):
    result = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[1].startswith("v"):
            result.add((fields[0], fields[1].removesuffix("/go.mod")))
    return result


def apply_replacements(required, text):
    replacements = {}
    for line in text.splitlines():
        line = line.split("//", 1)[0].strip().removeprefix("replace ")
        if "=>" not in line:
            continue
        old, new = [value.strip().split() for value in line.split("=>", 1)]
        if len(old) in {1, 2} and len(new) == 2 and new[1].startswith("v"):
            replacements[(old[0], old[1] if len(old) == 2 else None)] = tuple(new)
    return {replacements.get((m, v), replacements.get((m, None), (m, v))) for m, v in required}


async def prepare(args, remote, target, options, ssh_env):
    cwd = Path.cwd()
    origin = await run_process(["git", "remote", "get-url", "origin"], cwd, 15)
    if not re.search(r"[:/]larkvc/byteview-mp-media_platform(?:\.git)?$", origin["stdout"].strip()):
        raise ValueError("prepare Go cache in the matching Media workspace")
    head = await run_process(["git", "rev-parse", "HEAD"], cwd, 15)
    info = await remote({"action": "go-cache-info", "task": args.task})
    if head["stdout"].strip() != info["media_commit"]:
        raise ValueError("local and prepared Media source commits differ")
    main_mod = (cwd / "go.mod").read_text()
    required = declared_modules(main_mod)
    metadata = metadata_modules((cwd / "go.sum").read_text())
    sdk = cwd.parent / "byteview-mp-cpp-sdk"
    for filename in ["go.mod", "go.sum"]:
        view = await run_process(["git", "show", info["sdk_commit"] + ":" + filename], sdk, 15)
        if view["exit_code"]:
            raise ValueError("exact SDK module metadata is unavailable")
        if filename == "go.mod":
            required.update(declared_modules(view["stdout"]))
        else:
            metadata.update(metadata_modules(view["stdout"]))
    required = {(m, v) for m, v in required if m != "code.byted.org/larkvc/byteview-mp-cpp-sdk"}
    required = apply_replacements(required, main_mod)
    # CI Makefile使用的固定测试工具也只在Devbox私有bin目录编译安装。
    required.update(
        {("code.byted.org/tikcast/wallet_scripts/init-patcher", "v0.0.5"), ("gopkg.in/yaml.v3", "v3.0.1")}
    )
    if len(required) > 3000 or len(metadata) > 12000:
        raise ValueError("module graph exceeds workflow bounds")
    missing = []
    modules = [{"path": m, "version": v} for m, v in sorted(required)]
    # SSH命令行还携带固定worker，分批避免大模块图超过系统单参数限制。
    for offset in range(0, len(modules), 150):
        inventory = await remote(
            {"action": "go-cache-inventory", "task": args.task, "modules": modules[offset : offset + 150]}
        )
        missing.extend(inventory["missing"])
    go = shutil.which("go")
    if not go:
        raise ValueError("local Go is required to read or download module archives")
    environment = {**ssh_env, "GOWORK": "off", "GOFLAGS": "", "GIT_TERMINAL_PROMPT": "0"}
    cache = await run_process([go, "env", "GOMODCACHE"], args.state, 15, environment)
    if cache["exit_code"] or not cache["stdout"].strip():
        raise ValueError("local Go module cache metadata is unavailable")
    download = Path(cache["stdout"].strip()) / "cache/download"
    fetch = []
    for row in missing:
        module, version = row["path"], row["version"]
        base = download / escape_module(module) / "@v"
        if any(
            not (base / (escape_module(version) + suffix)).is_file() for suffix in [".mod", ".info", ".zip"]
        ):
            fetch.append(module + "@" + version)
    if fetch:
        # 下载源码归档，不在Mac编译/运行业务仓，也不修改正式go.mod。
        result = await run_process([go, "mod", "download", "-json", *fetch], args.state, 900, environment)
        if result["exit_code"]:
            raise RuntimeError("local Go archive preparation failed: " + result["stderr"][-1500:])
    files = {}
    for row in missing:
        for suffix in [".mod", ".info", ".zip"]:
            relative = escape_module(row["path"]) + "/@v/" + escape_module(row["version"]) + suffix
            path = download / relative
            if not path.is_file() or path.is_symlink():
                raise ValueError("expected module archive is missing: " + relative)
            files[relative] = path
    for module, version in required | metadata:
        relative = escape_module(module) + "/@v/" + escape_module(version) + ".mod"
        path = download / relative
        if path.is_file() and not path.is_symlink():
            files[relative] = path
    manifest = {
        "schema": 1,
        "media_commit": info["media_commit"],
        "sdk_commit": info["sdk_commit"],
        "module_count": len(required),
        "file_count": len(files),
        "files": {name: path.stat().st_size for name, path in sorted(files.items())},
    }
    archive = args.state / (args.task + "-go-cache-" + uuid.uuid4().hex + ".tar")
    payload = json.dumps(manifest, sort_keys=True).encode()
    import io

    with tarfile.open(archive, "w") as tar:
        entry = tarfile.TarInfo(".kaki-go-cache-manifest.json")
        entry.size, entry.mode = len(payload), 0o600
        tar.addfile(entry, io.BytesIO(payload))
        for relative, path in sorted(files.items()):
            tar.add(path, arcname=relative, recursive=False)
    archive.chmod(0o600)
    checksum = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    request = {
        "task": args.task,
        "bundle_sha256": checksum.hexdigest(),
        "media_commit": info["media_commit"],
        "sdk_commit": info["sdk_commit"],
    }
    uploaded = await remote({**request, "action": "go-cache-upload"})
    sent = await run_process(
        ["scp", "-q", *options, str(archive), target + ":" + uploaded["path"]], cwd, 900, ssh_env
    )
    if sent["exit_code"]:
        raise RuntimeError(sent["stderr"][-1500:])
    result = await remote({**request, "action": "go-cache-prepare"})
    result["local_archive"] = str(archive)
    return result
