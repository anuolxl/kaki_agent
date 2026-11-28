"""Bounded child-process execution shared by development capabilities."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from pathlib import Path

from kaki.types import KakiError


class ExecutionTimeout(KakiError):
    # 进程超时不表示已发生副作用被撤回；调用方需保留现场并判断未知结果。
    pass


def process_env(extra=None):
    # 子进程环境按白名单继承；这是减少意外传递凭据，不是文件系统或网络隔离。
    allowed = {
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "GOPATH",
        "GOROOT",
        "GOPROXY",
        "GOPRIVATE",
        "GONOSUMDB",
        "GONOPROXY",
        "CGO_ENABLED",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    }
    return {**{k: v for k, v in os.environ.items() if k in allowed}, **(extra or {})}


async def run_process(argv, cwd: Path, timeout=120, env=None):
    # 有界收集 stdout/stderr，超时/取消时尝试终止进程组；不使用 shell=True。
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=process_env(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError:
        raise KakiError("缺少可执行程序：" + str(argv[0])) from None

    async def capture(reader):
        pieces = []
        retained = 0
        discarded = 0
        while chunk := await reader.read(65536):
            room = max(0, 8 * 1024 * 1024 - retained)
            pieces.append(chunk[:room])
            retained += min(room, len(chunk))
            discarded += max(0, len(chunk) - room)
        return b"".join(pieces).decode(errors="replace"), discarded

    async def collect():
        out, err, _ = await asyncio.gather(capture(process.stdout), capture(process.stderr), process.wait())
        return {
            "argv": list(argv),
            "cwd": str(cwd),
            "exit_code": process.returncode,
            "stdout": out[0],
            "stderr": err[0],
            "discarded_bytes": {"stdout": out[1], "stderr": err[1]},
        }

    try:
        return await asyncio.wait_for(collect(), timeout)
    except BaseException as exc:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        if isinstance(exc, TimeoutError):
            raise ExecutionTimeout("进程超时，已终止进程组；有副作用的操作需要核对现场") from None
        raise
