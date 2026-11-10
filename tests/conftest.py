import asyncio
import subprocess

import pytest

from kaki.config import Config
from kaki.types import ModelResponse
from kaki.workspace import Workspace


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("KAKI_HOME", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return Workspace.resolve(repo, Config())


@pytest.fixture
def config():
    config = Config()
    config.runtime.checkpoint.policy = "never"
    return config


class ScriptedProvider:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.entered = asyncio.Event()
        self.block = None

    async def complete(self, messages, tools=None, **kwargs):
        self.calls.append((messages[:], tools, kwargs))
        self.entered.set()
        if self.block:
            await self.block.wait()
        if not self.responses:
            raise AssertionError("unexpected model call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response(messages, tools, kwargs)
        assert isinstance(response, ModelResponse)
        if not response.call_id:
            response.call_id = "model_" + str(len(self.calls))
        if kwargs.get("on_delta") and response.text:
            await kwargs["on_delta"](response.text, response.call_id)
        return response

    async def close(self):
        pass
