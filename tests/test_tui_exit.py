import asyncio

import pytest
from conftest import ScriptedProvider

from kaki.runtime import Runtime
from kaki.tui import KakiApp


@pytest.mark.asyncio
async def test_quit_posts_exit_without_mutating_textual_closing(workspace, config):
    runtime = Runtime(workspace, config, provider=ScriptedProvider())
    app = KakiApp(runtime)
    async with app.run_test() as pilot:
        await asyncio.wait_for(app.ready_event.wait(), 5)
        await app.action_quit_safe()
        # The framework sets this itself while handling ExitApp. Setting it early drops ExitApp.
        assert not app._closing
        await pilot.pause()
        assert app._exit
        assert runtime.closed


@pytest.mark.asyncio
async def test_ctrl_c_cancels_with_input_focused(workspace, config):
    from kaki.types import ModelResponse

    provider = ScriptedProvider(ModelResponse(text="not reached"))
    provider.block = asyncio.Event()
    runtime = Runtime(workspace, config, provider=provider)
    app = KakiApp(runtime)
    async with app.run_test() as pilot:
        await asyncio.wait_for(app.ready_event.wait(), 5)
        app.query_one("#prompt").value = "wait for model"
        await pilot.press("enter")
        await asyncio.wait_for(provider.entered.wait(), 5)
        await pilot.press("ctrl+c")
        await app.wait_for_turn(3)
        assert app.last_result["status"] == "cancelled"
