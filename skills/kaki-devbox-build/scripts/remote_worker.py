"""Devbox build worker (stdlib only, compatible with Python 3.5)."""

import datetime
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time

REPOS = {"sdk": "byteview-mp-cpp-sdk", "media": "byteview-mp-media_platform"}
DEPENDENCIES = {
    "log": {"repo": "cpputil/log", "ref": "1.0.0-streamlog"},
    "log_sdk": {"repo": "log_market/CppLogSDK", "ref": "master", "parent": "log"},
    "metrics2": {"repo": "cpputil/metrics2", "ref": "dev"},
    "metrics_sdk": {"repo": "cpputil/metrics_sdk", "ref": "v1.3.13"},
    "program": {"repo": "cpputil/program", "ref": "1.0.0"},
    "consul": {"repo": "cpputil/consul", "ref": "1.0.0", "parent": "program"},
    "monitoring": {"repo": "inf/monitoring-commons-cpp", "ref": "v1.0.30", "parent": "metrics_sdk"},
    "env": {"repo": "cpputil/env", "ref": "master", "parent": "metrics_sdk"},
}


def dependency_order():
    # 全新缓存也先校验/准备父BUILD，不能依赖上次执行遗留的父仓缓存。
    pending, result = set(DEPENDENCIES), []
    while pending:
        ready = sorted(name for name in pending if DEPENDENCIES[name].get("parent") not in pending)
        if not ready:
            raise RuntimeError("fixed dependency declarations contain a cycle")
        result.extend(ready)
        pending.difference_update(ready)
    return result


def dependency_ref(repository, version):
    # clone -b优先同名分支；裸rev-parse会优先同名tag，二者SHA可能不同。
    for prefix in ["refs/heads/", "refs/tags/"]:
        ref = prefix + version
        try:
            command(["git", "rev-parse", "--verify", ref + "^{commit}"], repository, timeout=15)
            return ref
        except RuntimeError:
            pass
    raise RuntimeError("fixed dependency branch/tag is missing")


def now():
    # 远端Python3.5也使用明确UTC时间，结果可以和本机审计时间对齐。
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def slug(value):
    # task/job只允许单个标识，不能借路径片段跳出专属目录。
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$", value or ""):
        raise ValueError("invalid task/job identifier")
    return value


def sha_file(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    # 以临时文件加replace提交状态，轮询方不读取半个JSON。
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = str(path) + ".tmp-" + str(os.getpid())
    with open(temp, "w") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
    os.chmod(temp, 0o600)
    os.replace(temp, str(path))


def read_json(path):
    with open(str(path)) as stream:
        return json.load(stream)


def command(argv, cwd=None, timeout=180, env=None):
    # 只接收内部构造的argv；超时与错误明确返回，不接受模型自由Shell。
    result = subprocess.run(
        [str(v) for v in argv],
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        timeout=timeout,
        env=env,
    )
    if result.returncode:
        raise RuntimeError("command failed ({}): {}".format(result.returncode, result.stderr[-1800:]))
    return result.stdout.strip()


def root_for(task):
    return pathlib.Path.home() / ".kaki-devbox" / "workspaces" / slug(task)


def owned_root(task):
    # 只认当前UID的Kaki专属根；现有陌生目录不接管、不清理。
    root = root_for(task)
    marker = root / ".kaki-owner.json"
    if root.is_symlink() or (root.exists() and not marker.is_file()):
        raise RuntimeError("existing directory has no Kaki ownership marker; not adopted")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not marker.exists():
        write_json(marker, {"schema": 1, "owner_uid": os.getuid(), "created": now()})
    if read_json(marker).get("owner_uid") != os.getuid():
        raise RuntimeError("workspace owner mismatch")
    return root


def upload_target(request):
    # 传输只面向两个已声明仓库和完整提交SHA，不复制未提交工作树或凭据。
    task, repo = slug(request["task"]), request["repo"]
    commit = request["commit"]
    if repo not in REPOS or not re.match(r"^[0-9a-f]{40}$", commit):
        raise ValueError("invalid repo/commit")
    directory = pathlib.Path.home() / ".kaki-devbox" / "transfers" / task
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return {"path": str(directory / (repo + "-" + commit + ".bundle"))}


def prepare(request):
    # 校验bundle SHA256并创建独立验证分支；HEAD不符或工作树脏时拒绝覆盖。
    root = owned_root(request["task"])
    repo, commit = request["repo"], request["commit"]
    if repo not in REPOS or not re.match(r"^[0-9a-f]{40}$", commit):
        raise ValueError("invalid repo/commit")
    source = pathlib.Path(upload_target(request)["path"])
    if sha_file(source) != request["bundle_sha256"]:
        raise RuntimeError("source bundle checksum mismatch")
    destination = root / REPOS[repo]
    branch = "codex/kaki-devbox-{0}-{1}".format(slug(request["task"]), repo)
    if destination.is_symlink():
        raise RuntimeError("validation checkout cannot be a symlink to another workspace")
    if not destination.exists():
        command(
            ["git", "-c", "core.hooksPath=/dev/null", "clone", str(source), str(destination)], timeout=600
        )
        command(["git", "checkout", "-b", branch, commit], destination)
        command(
            ["git", "remote", "set-url", "origin", "git@code.byted.org:larkvc/" + REPOS[repo] + ".git"],
            destination,
        )
    actual = command(["git", "rev-parse", "HEAD"], destination)
    actual_branch = command(["git", "symbolic-ref", "--short", "HEAD"], destination)
    dirty = command(["git", "status", "--porcelain", "--untracked-files=no"], destination)
    if actual != commit or dirty:
        raise RuntimeError("existing validation checkout differs or is dirty; use a new task directory")
    if actual_branch != branch:
        raise RuntimeError("validation branch changed; existing workspace was not modified")
    record = {
        "repo": repo,
        "path": str(destination),
        "branch": actual_branch,
        "source_commit": actual,
        "bundle_sha256": request["bundle_sha256"],
        "prepared": now(),
    }
    write_json(root / ("source-" + repo + ".json"), record)
    return record


def go_environment():
    # 选择现有Go1.23进程环境，不修改全局Go配置或默认Shell。
    env = os.environ.copy()
    home = pathlib.Path.home()
    paths = [shutil.which("go"), str(home / "go_path" / "go1.23" / "bin" / "go")]
    for directory in [home / "go", home / ".gvm" / "gos"]:
        if directory.is_dir():
            paths.extend(str(p / "bin" / "go") for p in directory.glob("go1.23*"))
    for value in paths:
        if value and pathlib.Path(value).is_file():
            version = command([value, "version"])
            if "go1.23." in version:
                env["PATH"] = str(pathlib.Path(value).parent) + os.pathsep + env.get("PATH", "")
                probe = env.copy()
                probe.pop("GOROOT", None)
                env["GOROOT"] = command([value, "env", "GOROOT"], env=probe)
                return env, version
    raise RuntimeError("Go 1.23.x was not found; no global Go configuration was changed")


def dependency_target(request):
    name = request["dependency"]
    if name not in DEPENDENCIES or not re.match(r"^[0-9a-f]{40}$", request["commit"]):
        raise ValueError("invalid fixed SDK dependency")
    root = owned_root(request["task"])
    directory = root / ".dependency-bundles"
    directory.mkdir(mode=0o700, exist_ok=True)
    return {"path": str(directory / (name + "-" + request["commit"] + ".bundle"))}


def prepare_dependency(request):
    # 仅导入SDK/固定父依赖已声明的Git源码；不把SSH凭据传给Devbox。
    root = owned_root(request["task"])
    expected = DEPENDENCIES[request["dependency"]]
    source = pathlib.Path(dependency_target(request)["path"])
    if sha_file(source) != request["bundle_sha256"]:
        raise RuntimeError("dependency bundle checksum mismatch")
    parent = root / ".git-dependencies"
    parent.mkdir(mode=0o700, exist_ok=True)
    destination = parent / (request["dependency"] + "-" + request["commit"] + ".git")
    if destination.is_symlink():
        raise RuntimeError("dependency mirror cannot be a symlink")
    if not destination.exists():
        command(["git", "-c", "core.hooksPath=/dev/null", "clone", "--bare", str(source), str(destination)])
    qualified_ref = dependency_ref(destination, expected["ref"])
    commit = command(["git", "rev-parse", qualified_ref + "^{commit}"], destination)
    if commit != request["commit"]:
        raise RuntimeError("dependency ref does not match transferred commit")
    record = {
        "repo": expected["repo"],
        "ref": expected["ref"],
        "qualified_ref": qualified_ref,
        "source_commit": commit,
        "bundle_sha256": request["bundle_sha256"],
        "path": str(destination),
    }
    manifest_path = root / "dependencies.json"
    manifest = read_json(manifest_path) if manifest_path.is_file() else {}
    manifest[request["dependency"]] = record
    write_json(manifest_path, manifest)
    return record


def dependency_environment(root, env):
    # URL替换只存在于本次构建子进程；全局Git配置和既有工程保持不变。
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = (
        "ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=2"
    )
    manifest_path = root / "dependencies.json"
    if not manifest_path.is_file():
        return
    entries = read_json(manifest_path)
    # Git 2.29尚不支持GIT_CONFIG_COUNT；保留git -c沿用的兼容参数协议。
    parameters = [env.get("GIT_CONFIG_PARAMETERS", "")]
    for name, expected in sorted(DEPENDENCIES.items()):
        if name not in entries:
            continue
        entry = entries[name]
        mirror = pathlib.Path(entry["path"])
        if str(mirror.parent) != str(root / ".git-dependencies") or mirror.is_symlink():
            raise RuntimeError("dependency mirror path changed")
        actual = command(["git", "rev-parse", dependency_ref(mirror, expected["ref"]) + "^{commit}"], mirror)
        if (
            entry["repo"] != expected["repo"]
            or entry["ref"] != expected["ref"]
            or actual != entry["source_commit"]
        ):
            raise RuntimeError("dependency mirror provenance changed")
        value = "url." + mirror.as_uri() + ".insteadOf=git@code.byted.org:" + expected["repo"]
        parameters.append("'" + value.replace("'", "'\\''") + "'")
    parameters.append("'protocol.file.allow=always'")
    env["GIT_CONFIG_PARAMETERS"] = " ".join(parameters).strip()


def build_plan(spec, root, jobdir):
    # 固定repo/mode映射到真实命令；Media用临时modfile链接本次SDK，正式依赖保持。
    repo, mode = spec["repo"], spec["mode"]
    if (
        repo not in REPOS
        or mode not in {"sdk-build", "sdk-test", "media-build", "media-test"}
        or not mode.startswith(repo + "-")
    ):
        raise ValueError("repo/mode mismatch")
    record = read_json(root / ("source-" + repo + ".json"))
    checkout = pathlib.Path(record["path"])
    if checkout.is_symlink() or str(checkout.resolve()) != str((root / REPOS[repo]).resolve()):
        raise RuntimeError("validation checkout path changed")
    if command(["git", "rev-parse", "HEAD"], checkout) != record["source_commit"]:
        raise RuntimeError("source HEAD changed")
    if command(["git", "symbolic-ref", "--short", "HEAD"], checkout) != record["branch"]:
        raise RuntimeError("source validation branch changed")
    if command(["git", "status", "--porcelain", "--untracked-files=no"], checkout):
        raise RuntimeError("source tracked files changed before build; not discarded")
    env, version, linked = os.environ.copy(), None, None
    if repo == "sdk":
        dependency_environment(root, env)
        official_blade = pathlib.Path("/opt/tiger/typhoon-blade/blade")
        if official_blade.is_file():
            # A project-local legacy Blade can precede the supported installation.
            # Change this build process only; do not rewrite the user's shell/PATH.
            env["PATH"] = str(official_blade.parent) + os.pathsep + env.get("PATH", "")
        argv = ["bash", "./build.sh"] + (["test"] if mode == "sdk-test" else [])
    else:
        env, version = go_environment()
        sdk_record = read_json(root / "source-sdk.json")
        sdk = pathlib.Path(sdk_record["path"])
        if sdk_record["source_commit"] != spec.get("sdk_commit"):
            raise RuntimeError("linked SDK commit mismatch")
        sdk_build = read_json(root / "sdk-build-artifacts.json")
        if sdk_build["source_commit"] != sdk_record["source_commit"]:
            raise RuntimeError("SDK artifact provenance differs from the linked source")
        artifact_root = pathlib.Path(sdk_build["artifact_root"])
        libraries = [artifact_root / "libcpp_sdk.so"]
        library = next((p for p in libraries if p.is_file()), None)
        if library is None:
            raise RuntimeError("SDK library missing: run sdk-build before linked Media verification")
        recorded = next((item for item in sdk_build["libraries"] if item["path"] == str(library)), None)
        if recorded is None or sha_file(library) != recorded["sha256"]:
            raise RuntimeError("SDK artifact checksum changed; linked Media build was not started")
        modfile = jobdir / "go.mod"
        shutil.copy2(str(checkout / "go.mod"), str(modfile))
        if (checkout / "go.sum").exists():
            shutil.copy2(str(checkout / "go.sum"), str(jobdir / "go.sum"))
        command(
            [
                str(pathlib.Path(env["GOROOT"]) / "bin" / "go"),
                "mod",
                "edit",
                "-modfile=" + str(modfile),
                "-replace=code.byted.org/larkvc/byteview-mp-cpp-sdk=" + str(sdk),
            ],
            checkout,
            env=env,
        )
        env["GOFLAGS"] = "-mod=mod -modfile=" + str(modfile)
        env["GOWORK"] = "off"
        env["CGO_ENABLED"] = "1"
        libdirs = [str(library.parent), "/opt/tiger/byteview/rtc/worker/lib"]
        env["LD_LIBRARY_PATH"] = os.pathsep.join(libdirs + [env.get("LD_LIBRARY_PATH", "")])
        env["CGO_LDFLAGS"] = "-L" + str(library.parent) + " " + env.get("CGO_LDFLAGS", "")
        includes = [artifact_root / "include", pathlib.Path("/opt/tiger/byteview/rtc/worker/lib/include")]
        env["CGO_CFLAGS"] = " ".join("-I" + str(p) for p in includes) + " " + env.get("CGO_CFLAGS", "")
        env["CGO_CXXFLAGS"] = env["CGO_CFLAGS"] + " " + env.get("CGO_CXXFLAGS", "")
        argv = (
            ["make", "test", "ARGS=./business_module/... -count=1"]
            if mode == "media-test"
            else ["go", "build", "-o", str(jobdir / "byteview_mp_media_platform"), "."]
        )
        linked = {
            "source_commit": sdk_record["source_commit"],
            "library": str(library),
            "sha256": sha_file(library),
            "modfile": str(modfile),
        }
    return checkout, argv, env, version, linked


def worker(jobfile):
    # 后台任务持有构建锁，记录源码与实参，等待真实退出码；SSH断开不取消worker。
    jobfile = pathlib.Path(jobfile)
    data = read_json(jobfile)
    root = root_for(data["task"])
    lock = open(str(root / ".build.lock"), "a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data.update(status="running", pid=os.getpid(), started=now())
        write_json(jobfile, data)
        checkout, argv, env, version, linked = build_plan(data, root, jobfile.parent)
        data.update(
            command=argv,
            cwd=str(checkout),
            go_version=version,
            linked_sdk=linked,
            source_commit=command(["git", "rev-parse", "HEAD"], checkout),
            blade_path=shutil.which("blade", path=env.get("PATH")),
        )
        write_json(jobfile, data)
        with open(str(jobfile.parent / "build.log"), "ab", 0) as log:
            if data["repo"] == "sdk":
                blade_root = checkout / "BLADE_ROOT.local"
                if blade_root.is_file():
                    shutil.copy2(str(blade_root), str(root / "BLADE_ROOT"))
                data["phase"] = "blade-init"
                write_json(jobfile, data)
                setup = subprocess.run(
                    ["blade", "init"],
                    cwd=str(root),
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=300,
                )
                if setup.returncode:
                    raise RuntimeError("blade init failed: " + str(setup.returncode))
            data["phase"] = "compile-or-test"
            process = subprocess.Popen(
                argv, cwd=str(checkout), env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
            data["child_pid"] = process.pid
            write_json(jobfile, data)
            try:
                code = process.wait(timeout=int(data.get("timeout", 1800)))
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise RuntimeError("remote build timeout; process group terminated")
        data.update(status="succeeded" if code == 0 else "failed", exit_code=code, ended=now())
        if code == 0 and data["mode"] == "sdk-build":
            # SDK test clears checkout/output. Keep build artifacts with this job
            # so later tests cannot remove the libraries used by linked Media.
            artifact_root = jobfile.parent / "sdk-output"
            shutil.copytree(str(checkout / "output"), str(artifact_root))
            artifacts = [p for p in artifact_root.glob("libcpp_sdk.*") if p.is_file()]
            data["artifacts"] = [
                {"path": str(p), "sha256": sha_file(p), "size": p.stat().st_size} for p in artifacts
            ]
            if not artifacts:
                data.update(
                    status="failed", error="build exited 0 but expected libcpp_sdk artifact is missing"
                )
            else:
                data["artifact_root"] = str(artifact_root)
                write_json(
                    root / "sdk-build-artifacts.json",
                    {
                        "schema": 1,
                        "job": data["job"],
                        "source_commit": data["source_commit"],
                        "artifact_root": str(artifact_root),
                        "libraries": data["artifacts"],
                    },
                )
        if code == 0 and data["mode"] == "media-build":
            binary = jobfile.parent / "byteview_mp_media_platform"
            data["artifacts"] = [
                {"path": str(binary), "sha256": sha_file(binary), "size": binary.stat().st_size}
            ]
        data["after_tracked_status"] = command(
            ["git", "status", "--porcelain", "--untracked-files=no"], checkout
        )
        if data["after_tracked_status"]:
            data.update(status="failed", error="build changed tracked source files; changes retained")
    except Exception as exc:
        data.update(status="failed", error=str(exc)[:1800], ended=now())
    finally:
        write_json(jobfile, data)
        lock.close()


def start(request, runner):
    # 同一job只启动一次；重复相同请求回已有记录，不同请求不能抢用job ID。
    root = owned_root(request["task"])
    slug(request["job"])
    if request["repo"] not in REPOS:
        raise ValueError("invalid repo")
    directory = root / ".kaki-jobs" / request["job"]
    if directory.exists():
        previous = read_json(directory / "status.json")
        if any(previous.get(key) != request.get(key) for key in ["repo", "mode", "sdk_commit"]):
            raise RuntimeError("job ID already belongs to a different request; query it without restarting")
        return status(request)
    directory.mkdir(parents=True)
    timeout = int(request.get("timeout", 1800))
    if timeout < 30 or timeout > 7200:
        raise ValueError("timeout must be 30..7200 seconds")
    data = {
        "schema": 1,
        "task": request["task"],
        "job": request["job"],
        "repo": request["repo"],
        "mode": request["mode"],
        "sdk_commit": request.get("sdk_commit"),
        "timeout": timeout,
        "status": "queued",
        "created": now(),
        "log_path": str(directory / "build.log"),
    }
    jobfile = directory / "status.json"
    write_json(jobfile, data)
    with open(str(directory / "worker.log"), "ab", 0) as log:
        process = subprocess.Popen(
            [sys.executable, str(runner), "--worker", str(jobfile)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    data["pid_hint"] = process.pid
    # 独立启动回执不覆盖可能已由worker提交的running/终态。
    write_json(directory / "launch.json", {"pid": process.pid})
    return data


def status(request):
    # 读取同一job的终态和有界日志；PID消失只标未知，不当成功，也不重新编译。
    directory = root_for(request["task"]) / ".kaki-jobs" / slug(request["job"])
    jobfile = directory / "status.json"
    wait = min(30, max(0, int(request.get("wait", 0))))
    deadline = time.time() + wait
    while True:
        data = read_json(jobfile)
        launch = directory / "launch.json"
        pid = data.get("pid") or (read_json(launch).get("pid") if launch.is_file() else None)
        if data["status"] in {"queued", "running"} and pid:
            proc = pathlib.Path("/proc") / str(pid) / "cmdline"
            if proc.exists() and str(jobfile).encode("utf-8") not in proc.read_bytes():
                data.update(
                    status="unknown", error="worker PID no longer belongs to this job; do not replay start"
                )
            elif not proc.exists() and sys.platform.startswith("linux"):
                data.update(
                    status="unknown",
                    error="worker disappeared without a terminal receipt; inspect the same job",
                )
        if data["status"] not in {"queued", "running"} or time.time() >= deadline:
            break
        time.sleep(1)
    data["log_path"] = str(directory / "build.log")
    logfile = directory / "build.log"
    if logfile.exists():
        with open(str(logfile), "rb") as stream:
            stream.seek(max(0, logfile.stat().st_size - 16000))
            lines = stream.read().decode("utf-8", "replace").splitlines()
        data["log_tail"] = lines[-min(120, max(1, int(request.get("lines", 40)))) :]
    return data


def dispatch(request, runner):
    # 远端只暴露固定工作流动作，发布SDK、部署和合入不属于此协议。
    action = request["action"]
    if action == "upload":
        return upload_target(request)
    if action == "prepare":
        return prepare(request)
    if action == "dependency-upload":
        return dependency_target(request)
    if action == "dependency-prepare":
        return prepare_dependency(request)
    if action == "start":
        return start(request, runner)
    if action == "status":
        return status(request)
    raise ValueError("unknown remote action")


if __name__ == "__main__" and sys.argv[1:2] == ["--worker"]:
    worker(sys.argv[2])
