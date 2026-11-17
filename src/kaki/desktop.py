"""Native macOS WebKit window over the same authenticated local App used in browsers."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import select
import signal
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import httpx

from kaki.app_controller import AppController
from kaki.app_server import AppServer
from kaki.config import kaki_home, load_config, safe_error
from kaki.types import KakiError


class Instance:
    def __init__(self):
        self.folder = kaki_home() / "app"
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.file = (self.folder / "instance.lock").open("a+")
        self.path = self.folder / "instance.json"
        self.owner = False

    def acquire(self):
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.owner = True
            return True
        except BlockingIOError:
            return False

    def write(self, server):
        data = {"schema_version": 1, "pid": os.getpid(), "origin": server.origin, "token": server.secret}
        temp = self.path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
        os.replace(temp, self.path)

    def close(self):
        if self.owner:
            with contextlib.suppress(OSError, ValueError):
                if json.loads(self.path.read_text()).get("pid") == os.getpid():
                    self.path.unlink()
            fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()


def choose_path(cwd, config):
    if cwd:
        return Path(cwd).expanduser().resolve()
    if config.agents.defaults.workspace:
        return Path(config.agents.defaults.workspace).expanduser()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5
    )
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def reuse(instance, path, browser):
    try:
        info = json.loads(instance.path.read_text())
        url = info["origin"]
        # The private capability is local-only; reject corrupt or redirected instance records.
        if not url.startswith("http://127.0.0.1:") or "/" in url.removeprefix("http://127.0.0.1:"):
            raise ValueError("Invalid App instance")
        response = httpx.post(
            url + "/api/focus",
            json={"path": str(path) if path else None},
            headers={"Authorization": "Bearer " + info["token"]},
            timeout=15,
        )
        response.raise_for_status()
        if browser:
            webbrowser.open(url + "/connect/" + info["token"])
        print("Kaki App已经运行，已切换到现有窗口。", flush=True)
    except (OSError, ValueError, KeyError, httpx.HTTPError):
        raise KakiError("Kaki App正在启动或退出，请稍后重试；没有绕过实例锁另起进程") from None


async def serve(instance, config, path, resume, port, *, on_ready=None):
    server = AppServer(AppController(config), port=port, initial_path=path, resume=resume)
    try:
        await server.start()
        instance.write(server)
        print(f"Kaki App已启动：{server.origin} · Ctrl+C退出进程", flush=True)
        if on_ready:
            on_ready(server)
        await server.stop_event.wait()
    finally:
        await server.close()


class NativeService:
    def __init__(self, instance, config, path, resume, port):
        self.args = (instance, config, path, resume, port)
        self.ready, self.done = threading.Event(), threading.Event()
        self.loop = None
        self.server = None
        self.error = None
        self.thread = threading.Thread(target=self._run, name="kaki-app-runtime", daemon=True)

    def _run(self):
        async def run():
            self.loop = asyncio.get_running_loop()

            def ready(server):
                self.server = server
                self.ready.set()

            await serve(*self.args, on_ready=ready)

        try:
            asyncio.run(run())
        except BaseException as exc:
            self.error = safe_error(exc)
        finally:
            self.ready.set()
            self.done.set()

    def stop(self):
        if self.loop and self.server and not self.done.is_set():
            self.loop.call_soon_threadsafe(self.server.stop_event.set)


def native_window(instance, config, path, resume, port):
    import webview
    from PyObjCTools import AppHelper, MachSignals
    from webview.menu import Menu, MenuAction

    service = NativeService(instance, config, path, resume, port)
    service.thread.start()
    if not service.ready.wait(20) or not service.server:
        service.stop()
        raise KakiError(service.error or "App本地服务启动超时")
    window = webview.create_window(
        "Kaki",
        service.server.connect_url,
        width=1280,
        height=860,
        min_size=(900, 640),
        background_color="#FFFFFF",
        text_select=True,
    )

    def closing():
        service.stop()
        # Cocoa's terminate action otherwise exits before asyncio can persist cancellation.
        # The monitor closes the window after the service has actually finished.
        return service.done.is_set()

    window.events.closing += closing
    service.server.on_focus = lambda: (window.restore(), window.show())

    def select_workspace():
        selected = window.create_file_dialog(webview.FOLDER_DIALOG)
        if selected:
            window.evaluate_js("window.kakiOpenWorkspace(" + json.dumps(selected[0]) + ")")

    def monitor():
        service.done.wait()
        with contextlib.suppress(Exception):
            window.destroy()

    # Cocoa can hold the main thread in its native loop. A signal wakeup pipe makes Ctrl+C
    # observable immediately by a Python watcher instead of waiting for the next Cocoa callback.
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    previous_fd = signal.set_wakeup_fd(write_fd)
    previous = {s: signal.signal(s, lambda *_: None) for s in (signal.SIGINT, signal.SIGTERM)}
    watching = threading.Event()
    exit_code = [0]

    def signals():
        while not watching.is_set():
            ready, _, _ = select.select([read_fd], [], [], 0.2)
            if ready:
                values = os.read(read_fd, 128)
                if not values:
                    break
                if signal.SIGINT in values:
                    exit_code[0] = 130
                service.stop()
                break

    def install_cocoa_signals():
        def stop(signum):
            if signum == signal.SIGINT:
                exit_code[0] = 130
            service.stop()

        # pywebview installs a Mach SIGINT handler immediately before NSApp.run().
        # Register on that run loop afterwards so its default terminate_ cannot bypass cleanup.
        for sig in (signal.SIGINT, signal.SIGTERM):
            MachSignals.signal(sig, stop)

    AppHelper.callAfter(install_cocoa_signals)

    watcher = threading.Thread(target=signals, name="kaki-app-signals", daemon=True)
    watcher.start()
    menu = [
        Menu(
            "Kaki",
            [MenuAction("打开仓库…", select_workspace), MenuAction("退出 Kaki", service.stop)],
        )
    ]
    try:
        webview.start(monitor, private_mode=True, debug=False, menu=menu)
    finally:
        watching.set()
        service.stop()
        service.thread.join(20)
        signal.set_wakeup_fd(previous_fd)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        watcher.join(1)
        os.close(read_fd)
        os.close(write_fd)
    if service.error:
        raise KakiError(service.error)
    return exit_code[0]


def launch(cwd=None, config_path=None, resume=None, model=None, *, browser=False, no_open=False, port=0):
    config = load_config(config_path, model=model)
    path = choose_path(cwd, config)
    instance = Instance()
    try:
        if not instance.acquire():
            reuse(instance, path, browser)
            return 0
        if sys.platform == "darwin" and not browser and not no_open:
            return native_window(instance, config, path, resume, port)

        def ready(server):
            if not no_open:
                webbrowser.open(server.connect_url)

        try:
            asyncio.run(serve(instance, config, path, resume, port, on_ready=ready))
        except KeyboardInterrupt:
            return 130
        return 0
    finally:
        instance.close()


def main():
    parser = argparse.ArgumentParser(description="Kaki本机会话App")
    parser.add_argument("-C", "--cwd", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--resume")
    parser.add_argument("--model")
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    try:
        code = launch(
            args.cwd,
            args.config,
            args.resume,
            args.model,
            browser=args.browser,
            no_open=args.no_open,
            port=args.port,
        )
    except (KakiError, OSError, ValueError) as exc:
        print("Kaki App: " + safe_error(exc), file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
