"""Verify OS process exit and singleton behavior, not just cancellation of a Python Task."""

import json
import os
import signal
import subprocess
import sys
import time

import httpx
import pytest


@pytest.mark.parametrize(
    "native",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.skipif(
                sys.platform != "darwin" or os.environ.get("KAKI_TEST_NATIVE") != "1",
                reason="native Cocoa window requires an explicit macOS desktop test",
            ),
        ),
    ],
)
def test_app_sigint_exits_server_and_releases_singleton(workspace, tmp_path, native):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"providers": {"openai": {"apiKey": "fake-no-model-call"}}}))
    log = (tmp_path / "app.log").open("w")
    args = [
        sys.executable,
        "-m",
        "kaki.desktop",
        "--config",
        str(config),
        "-C",
        str(workspace.root),
    ]
    if not native:
        args.append("--no-open")
    process = subprocess.Popen(args, stdout=log, stderr=log, env=os.environ.copy(), start_new_session=True)
    record = tmp_path / "state/app/instance.json"
    info = None
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError((tmp_path / "app.log").read_text())
            if record.exists():
                try:
                    info = json.loads(record.read_text())
                    if httpx.get(info["origin"] + "/health", timeout=1, trust_env=False).status_code == 200:
                        break
                except (ValueError, httpx.HTTPError):
                    pass
            time.sleep(0.05)
        assert info is not None, "App did not start"
        second = subprocess.run(args, capture_output=True, text=True, timeout=10)
        assert second.returncode == 0 and json.loads(record.read_text())["pid"] == process.pid
        process.send_signal(signal.SIGINT)
        process.wait(timeout=10)
        assert process.returncode == 130
        assert not record.exists()
        try:
            httpx.get(info["origin"] + "/health", timeout=1, trust_env=False)
        except httpx.ConnectError:
            pass
        else:
            raise AssertionError("HTTP server still running after Ctrl+C")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        log.close()
