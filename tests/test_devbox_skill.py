import ast
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/kaki-devbox-build/scripts"


def load(name):
    spec = importlib.util.spec_from_file_location("kaki_devbox_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(path, *args):
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, stderr=subprocess.DEVNULL
    ).strip()


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    remote = load("remote_worker")
    monkeypatch.setattr(remote.pathlib.Path, "home", classmethod(lambda cls: tmp_path / "devbox-home"))
    tools = tmp_path / "tools"
    tools.mkdir()
    blade = tools / "blade"
    blade.write_text("#!/bin/sh\nmkdir -p .blade_tools\necho initialized\n")
    blade.chmod(0o755)
    import os

    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    (source / "build.sh").write_text(
        "#!/bin/sh\nif [ x$1 = xtest ]; then rm -rf output; echo tests-passed; exit 0; fi\nmkdir -p output\nprintf 'sdk bytes' > output/libcpp_sdk.so\necho compiled\n"
    )
    (source / "input.txt").write_text("immutable source")
    git(source, "add", "build.sh", "input.txt")
    git(source, "commit", "-qm", "fixture")
    git(source, "branch", "-M", "codex/source")
    commit = git(source, "rev-parse", "HEAD")
    request = {"task": "test-rtc", "repo": "sdk", "commit": commit}
    target = Path(remote.upload_target(request)["path"])
    git(source, "bundle", "create", str(target), "refs/heads/codex/source")
    request["bundle_sha256"] = remote.sha_file(target)
    return remote, source, request


def test_bundle_prepare_isolated_exact_and_preserves_original_index(prepared):
    remote, source, request = prepared
    head, index = (source / ".git/HEAD").read_bytes(), (source / ".git/index").read_bytes()
    result = remote.prepare(request)
    target = Path(result["path"])
    assert result["source_commit"] == request["commit"]
    assert result["branch"].startswith("codex/kaki-devbox-")
    assert (target / "input.txt").read_text() == "immutable source"
    assert (source / ".git/HEAD").read_bytes() == head
    assert (source / ".git/index").read_bytes() == index
    assert remote.prepare(request)["source_commit"] == request["commit"]
    (target / "input.txt").write_text("user edit")
    with pytest.raises(RuntimeError, match="dirty"):
        remote.prepare(request)
    assert (target / "input.txt").read_text() == "user edit"


def test_owned_directory_and_job_identity_fail_closed(prepared, monkeypatch):
    remote, _, request = prepared
    foreign = remote.root_for("foreign")
    foreign.mkdir(parents=True)
    (foreign / "user.txt").write_text("keep")
    with pytest.raises(RuntimeError, match="ownership"):
        remote.owned_root("foreign")
    with pytest.raises(ValueError):
        remote.slug("../master")
    root = remote.owned_root(request["task"])
    file = root / ".kaki-jobs" / "same" / "status.json"
    old = {
        "task": request["task"],
        "job": "same",
        "repo": "sdk",
        "mode": "sdk-build",
        "sdk_commit": None,
        "status": "succeeded",
        "exit_code": 0,
    }
    remote.write_json(file, old)
    monkeypatch.setattr(
        remote.subprocess, "Popen", lambda *a, **k: pytest.fail("completed job must not restart")
    )
    assert remote.start(old, SCRIPTS / "remote_worker.py")["status"] == "succeeded"
    with pytest.raises(RuntimeError, match="different request"):
        remote.start({**old, "mode": "sdk-test"}, SCRIPTS / "remote_worker.py")
    assert (foreign / "user.txt").read_text() == "keep"


def test_validation_branch_and_checkout_symlink_are_not_adopted(prepared):
    remote, _, request = prepared
    record = remote.prepare(request)
    target = Path(record["path"])
    git(target, "checkout", "-b", "master")
    with pytest.raises(RuntimeError, match="branch changed"):
        remote.prepare(request)
    assert git(target, "branch", "--show-current") == "master"
    root = remote.owned_root("linked")
    (root / "byteview-mp-cpp-sdk").symlink_to(target, target_is_directory=True)
    linked = {**request, "task": "linked"}
    shutil.copy2(remote.upload_target(request)["path"], remote.upload_target(linked)["path"])
    with pytest.raises(RuntimeError, match="symlink"):
        remote.prepare(linked)


def test_worker_records_real_exit_log_and_commit(prepared):
    remote, _, request = prepared
    remote.prepare(request)
    root = remote.root_for(request["task"])
    (root / ".blade_tools").mkdir()
    file = root / ".kaki-jobs" / "compile" / "status.json"
    remote.write_json(
        file,
        {
            "task": request["task"],
            "job": "compile",
            "repo": "sdk",
            "mode": "sdk-build",
            "status": "queued",
            "timeout": 30,
        },
    )
    remote.worker(str(file))
    result = remote.status({"task": request["task"], "job": "compile"})
    assert result["status"] == "succeeded" and result["exit_code"] == 0
    assert result["source_commit"] == request["commit"]
    assert result["command"] == ["bash", "./build.sh"]
    assert "compiled" in result["log_tail"]
    assert Path(result["cwd"], "output/libcpp_sdk.so").read_bytes() == b"sdk bytes"
    manifest = remote.read_json(root / "sdk-build-artifacts.json")
    saved_library = Path(manifest["libraries"][0]["path"])
    testfile = root / ".kaki-jobs" / "unit" / "status.json"
    remote.write_json(
        testfile,
        {
            "task": request["task"],
            "job": "unit",
            "repo": "sdk",
            "mode": "sdk-test",
            "status": "queued",
            "timeout": 30,
        },
    )
    remote.worker(str(testfile))
    assert remote.read_json(testfile)["exit_code"] == 0
    assert not Path(result["cwd"], "output").exists()
    assert saved_library.read_bytes() == b"sdk bytes"


def test_remote_worker_keeps_python35_syntax_and_no_local_stdlib_shadow():
    ast.parse((SCRIPTS / "remote_worker.py").read_text(), feature_version=(3, 5))
    assert not (SCRIPTS / "inspect.py").exists()
    probe = load("devbox_probe")
    ast.parse(probe.REMOTE, feature_version=(3, 5))


def test_dependency_bundle_mirror_redirect_is_process_local(prepared):
    remote, source, request = prepared
    remote.prepare(request)
    ref = remote.DEPENDENCIES["program"]["ref"]
    git(source, "tag", ref)
    payload = {"task": request["task"], "dependency": "program", "commit": request["commit"]}
    bundle = Path(remote.dependency_target(payload)["path"])
    git(source, "bundle", "create", str(bundle), "--all")
    payload["bundle_sha256"] = remote.sha_file(bundle)
    record = remote.prepare_dependency(payload)
    assert record["source_commit"] == request["commit"] and record["ref"] == ref
    import os

    env = os.environ.copy()
    remote.dependency_environment(remote.root_for(request["task"]), env)
    target = remote.root_for(request["task"]) / "downloaded-program"
    subprocess.run(
        ["git", "clone", "-q", "--depth=1", "-b", ref, "git@code.byted.org:cpputil/program", str(target)],
        env=env,
        check=True,
    )
    assert git(target, "rev-parse", "HEAD") == request["commit"]
    assert "GIT_CONFIG_PARAMETERS" in env
    assert "GIT_CONFIG_PARAMETERS" not in os.environ
    assert git(source, "status", "--porcelain") == ""


def test_dependency_order_prepares_parent_before_reading_its_build(prepared):
    remote, _, _ = prepared
    order = remote.dependency_order()
    assert set(order) == set(remote.DEPENDENCIES)
    assert order.index("program") < order.index("consul")
    assert order.index("log") < order.index("log_sdk")


def test_dependency_version_prefers_branch_like_git_clone_not_ambiguous_tag(prepared):
    remote, source, _ = prepared
    old = git(source, "rev-parse", "HEAD")
    git(source, "tag", "1.0.0")
    (source / "input.txt").write_text("branch version")
    git(source, "add", "input.txt")
    git(source, "commit", "-qm", "new branch version")
    git(source, "branch", "1.0.0")
    selected = remote.dependency_ref(source, "1.0.0")
    assert selected == "refs/heads/1.0.0"
    assert git(source, "rev-parse", selected) != old


def test_bpt_parent_declaration_is_verified_before_reporting_success(prepared):
    remote, _, request = prepared
    root = remote.owned_root(request["task"])
    remote.write_json(root / "dependencies.json", {"sonic": {}})
    assert remote.bpt_declarations(root)["sonic"]["sha256"] is None
    with pytest.raises(RuntimeError, match="not installed"):
        remote.bpt_declarations(root, require=True)
    build = root / "cpp3rdlib/rapidjson/BUILD"
    build.parent.mkdir(parents=True)
    build.write_text('deps=["middleware/sonic-cpp:1.1.0@//middleware/sonic-cpp:sonic-cpp"]')
    assert remote.bpt_declarations(root, require=True)["sonic"]["sha256"] == remote.sha_file(build)
    build.write_text('deps=["middleware/sonic-cpp:2.0.0@//middleware/sonic-cpp:sonic-cpp"]')
    with pytest.raises(RuntimeError, match="no longer declares"):
        remote.bpt_declarations(root, require=True)


def test_queued_worker_disappearance_is_unknown_and_does_not_restart(prepared, monkeypatch):
    remote, _, request = prepared
    root = remote.owned_root(request["task"])
    directory = root / ".kaki-jobs" / "gone"
    remote.write_json(directory / "status.json", {"status": "queued"})
    remote.write_json(directory / "launch.json", {"pid": 999999999})
    monkeypatch.setattr(remote.sys, "platform", "linux")
    result = remote.status({"task": request["task"], "job": "gone"})
    assert result["status"] == "unknown"
    assert "without a terminal receipt" in result["error"]


def test_linked_media_rejects_changed_sdk_artifact(prepared, monkeypatch):
    remote, source, request = prepared
    remote.prepare(request)
    root = remote.root_for(request["task"])
    media = root / remote.REPOS["media"]
    subprocess.run(["git", "clone", "-q", str(source), str(media)], check=True)
    branch = git(media, "branch", "--show-current")
    remote.write_json(
        root / "source-media.json",
        {"path": str(media), "source_commit": request["commit"], "branch": branch},
    )
    library = root / "artifacts/libcpp_sdk.so"
    library.parent.mkdir()
    library.write_bytes(b"built library")
    remote.write_json(
        root / "sdk-build-artifacts.json",
        {
            "source_commit": request["commit"],
            "artifact_root": str(library.parent),
            "libraries": [{"path": str(library), "sha256": remote.sha_file(library)}],
        },
    )
    library.write_bytes(b"changed after build")
    monkeypatch.setattr(remote, "go_environment", lambda: ({}, "go1.23.12"))
    with pytest.raises(RuntimeError, match="checksum changed"):
        remote.build_plan(
            {"repo": "media", "mode": "media-build", "sdk_commit": request["commit"]},
            root,
            root / "job",
        )


async def test_adapter_rejects_target_injection_before_ssh(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    workflow = load("workflow")
    monkeypatch.setenv("KAKI_DEVBOX_TARGET", "user@host; touch bad")
    args = workflow.parser().parse_args(
        ["--action", "status", "--state", str(tmp_path), "--task", "rtc", "--job", "test"]
    )
    with pytest.raises(ValueError, match="TARGET"):
        await workflow.main(args)


async def test_adapter_prepare_reaches_remote_clone_after_bundle_creation(
    prepared, tmp_path, monkeypatch, capsys
):
    import json
    import shlex

    remote, source, request = prepared
    monkeypatch.syspath_prepend(str(SCRIPTS))
    workflow = load("workflow")
    monkeypatch.chdir(source)
    monkeypatch.setenv("KAKI_DEVBOX_TARGET", "user@host")
    git(source, "remote", "add", "origin", "git@code.byted.org:larkvc/byteview-mp-cpp-sdk.git")
    original = workflow.run_process
    actions = []

    async def transport(argv, cwd, timeout=120, env=None):
        if argv[0] == "ssh":
            inner = shlex.split(argv[-1])[-1]
            payload = json.loads(shlex.split(inner)[-1])
            actions.append(payload["action"])
            result = remote.dispatch(payload, SCRIPTS / "remote_worker.py")
            return {
                "exit_code": 0,
                "stdout": workflow.MARKER + json.dumps({"ok": True, "result": result}),
                "stderr": "",
            }
        if argv[0] == "scp":
            shutil.copy2(argv[-2], argv[-1].split(":", 1)[1])
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        return await original(argv, cwd, timeout, env)

    monkeypatch.setattr(workflow, "run_process", transport)
    args = workflow.parser().parse_args(
        [
            "--action",
            "prepare",
            "--state",
            str(tmp_path / "state"),
            "--repo",
            "sdk",
            "--branch",
            "codex/source",
            "--commit",
            request["commit"],
            "--task",
            "adapter-test",
        ]
    )
    await workflow.main(args)
    result = json.loads(capsys.readouterr().out)
    assert actions == ["upload", "prepare"]
    assert result["result"]["source_commit"] == request["commit"]
    assert Path(result["result"]["path"], "input.txt").read_text() == "immutable source"
