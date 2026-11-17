import asyncio

import httpx
import pytest
from aiohttp import ClientSession
from conftest import ScriptedProvider

from kaki.app_controller import AppController
from kaki.app_server import AppServer
from kaki.runtime import Runtime
from kaki.types import ModelResponse


@pytest.mark.asyncio
async def test_auth_origin_cookie_crud_and_no_secret_exposure(workspace, config):
    app = AppController(config, runtime_factory=lambda w, c: Runtime(w, c, provider=ScriptedProvider()))
    server = await AppServer(app, initial_path=workspace.root).start()
    try:
        async with httpx.AsyncClient(
            base_url=server.origin, follow_redirects=True, trust_env=False
        ) as client:
            assert (await client.get("/api/state")).status_code == 401
            landing = await client.get("/connect/" + server.secret)
            assert landing.status_code == 200 and server.secret not in landing.text
            cookie_header = landing.history[0].headers.get_list("set-cookie")[0]
            assert "HttpOnly" in cookie_header and "SameSite=Strict" in cookie_header
            info = (await client.get("/api/state")).json()
            wid = info["initial"]["workspace_id"]
            sid = info["initial"]["session_id"]
            assert "api_key" not in info and "apiKey" not in info
            endpoint = f"/api/workspaces/{wid}/sessions/{sid}"
            assert (
                await client.patch(
                    endpoint,
                    json={"title": "bad"},
                    headers={"Origin": "https://evil.example", "X-Kaki-Client": "1"},
                )
            ).status_code == 403
            assert (await client.patch(endpoint, json={"title": "no origin"})).status_code == 403
            headers = {"Origin": server.origin, "X-Kaki-Client": "1"}
            assert (
                await client.patch(endpoint, json={"title": "App对话"}, headers=headers)
            ).status_code == 200
            assert (await client.get(endpoint)).json()["session"]["title"] == "App对话"
            assert (await client.get("/assets/../config.json")).status_code != 200
            assert (await client.get("/api/state", headers={"Host": "evil.example"})).status_code == 403
            assert (await client.patch(endpoint, json={"archived": True}, headers=headers)).status_code == 200
            assert (await client.get(f"/api/workspaces/{wid}/sessions?archived=1")).json()["sessions"][0][
                "id"
            ] == sid
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_websocket_reports_running_and_completed_from_real_controller(workspace, config):
    provider = ScriptedProvider(ModelResponse(text="done"))
    provider.block = asyncio.Event()
    app = AppController(config, runtime_factory=lambda w, c: Runtime(w, c, provider=provider))
    server = await AppServer(app, initial_path=workspace.root).start()
    try:
        headers = {"Authorization": "Bearer " + server.secret, "Origin": server.origin}
        async with ClientSession(headers=headers) as client:
            async with client.ws_connect(server.origin + "/api/events") as ws:
                assert (await ws.receive_json())["type"] == "connected"
                e = app.engine(server.initial["workspace_id"])
                await app.send(workspace.id, e.session, "hello")
                await asyncio.wait_for(provider.entered.wait(), 5)
                message = await ws.receive_json(timeout=5)
                assert message["type"] == "workspace.changed" and message["live"]["busy"]
                provider.block.set()
                await e.task
                async with asyncio.timeout(5):
                    while True:
                        msg = await ws.receive_json()
                        if msg.get("live", {}).get("phase") == "completed":
                            assert not msg["live"]["busy"]
                            break
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_slow_viewer_gets_resync_not_fake_completion(config):
    c = AppController(config)
    q = asyncio.Queue(maxsize=1)
    c.subscribers.add(q)
    c.publish("first")
    c.publish("second")
    assert q.get_nowait()["type"] == "resync"
