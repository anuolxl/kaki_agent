"""Exercise a real terminal driver; Pilot teardown alone cannot prove CLI exit."""

import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time


def test_installed_cli_quits_from_real_pty(workspace, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"providers": {"openai": {"apiKey": "test-no-network"}}}))
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    process = subprocess.Popen(
        [sys.executable, "-m", "kaki", "--config", str(config), "-C", str(workspace.root)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"},
        start_new_session=True,
    )
    os.close(slave)
    seen = b""
    sent = False
    deadline = time.monotonic() + 12
    try:
        while time.monotonic() < deadline and process.poll() is None:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    seen += os.read(master, 65536)
                except OSError:
                    break
            if not sent and "就绪".encode() in seen:
                os.write(master, b"/quit\r")
                sent = True
        assert sent, "CLI did not reach ready state"
        process.wait(timeout=2)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        os.close(master)
