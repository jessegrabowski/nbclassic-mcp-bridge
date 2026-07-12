import asyncio
import json
import os
import subprocess
import sys

import pytest
from conftest import free_port, wait_until_up
from tornado.httpclient import AsyncHTTPClient, HTTPClientError
from tornado.websocket import websocket_connect

from nbclassic_mcp_bridge.switchboard import PROTOCOL_VERSION


TOKEN = "integration-test-token"


@pytest.fixture(scope="module")
def relay_url():
    """Run a Jupyter server with the extension loaded; yield the relay WebSocket URL."""
    port = free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "jupyter",
            "server",
            "--port",
            str(port),
            "--no-browser",
            "--ServerApp.jpserver_extensions={'nbclassic_mcp_bridge':True}",
        ],
        env={**os.environ, "JUPYTER_TOKEN": TOKEN},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_until_up(port, TOKEN)
        yield f"ws://localhost:{port}/mcp-bridge"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def hello(role, notebook="nb.ipynb"):
    return json.dumps({"kind": "hello", "protocol": PROTOCOL_VERSION, "role": role, "notebook": notebook})


async def _recv(ws):
    return json.loads(await asyncio.wait_for(ws.read_message(), timeout=5))


def test_hello_roundtrip_over_real_sockets(relay_url):
    async def scenario():
        url = f"{relay_url}?token={TOKEN}"
        ext = await websocket_connect(url)
        mcp = await websocket_connect(url)
        ext.write_message(hello("extension"))
        mcp.write_message(hello("mcp"))

        assert (await _recv(ext))["state"] == "joined"
        assert (await _recv(mcp))["state"] == "joined"

        cmd = {"kind": "cmd", "id": 1, "op": "snapshot", "args": {}}
        mcp.write_message(json.dumps(cmd))
        assert await _recv(ext) == cmd

        ext.close()
        mcp.close()

    asyncio.run(scenario())


def test_rooms_endpoint_reports_live_presence(relay_url):
    # Assertions are scoped to this test's own notebook: the module-scoped server is shared, so
    # other tests' rooms may still be draining when this one polls.
    async def scenario():
        http_url = relay_url.replace("ws://", "http://") + f"/rooms?token={TOKEN}"
        client = AsyncHTTPClient()

        async def rooms_entry(deadline_s=5, until=lambda entry: entry is not None):
            deadline = asyncio.get_event_loop().time() + deadline_s
            entry = None
            while asyncio.get_event_loop().time() < deadline:
                entry = json.loads((await client.fetch(http_url)).body)["rooms"].get("open.ipynb")
                if until(entry):
                    break
                await asyncio.sleep(0.05)
            return entry

        before = json.loads((await client.fetch(http_url)).body)
        assert "open.ipynb" not in before["rooms"]

        ext = await websocket_connect(f"{relay_url}?token={TOKEN}")
        ext.write_message(hello("extension", "open.ipynb"))
        assert await rooms_entry() == ["extension"]

        ext.close()
        assert await rooms_entry(until=lambda entry: entry is None) is None

    asyncio.run(scenario())


def test_rooms_endpoint_requires_auth(relay_url):
    async def scenario():
        http_url = relay_url.replace("ws://", "http://") + "/rooms"
        with pytest.raises(HTTPClientError) as exc_info:
            await AsyncHTTPClient().fetch(http_url)
        assert exc_info.value.code == 403

    asyncio.run(scenario())


def test_events_missed_while_detached_replay_on_rejoin(relay_url):
    async def scenario():
        url = f"{relay_url}?token={TOKEN}"
        ext = await websocket_connect(url)
        ext.write_message(hello("extension", "replay.ipynb"))

        # Nobody is listening while these happen.
        for cell_id in ("a", "b"):
            ext.write_message(json.dumps({"kind": "event", "name": "cell_created", "data": {"cell_id": cell_id}}))

        first = await websocket_connect(url)
        first.write_message(hello("mcp", "replay.ipynb"))
        assert (await _recv(first))["state"] == "joined"
        replayed = [await _recv(first), await _recv(first)]
        assert [event["data"]["cell_id"] for event in replayed] == ["a", "b"]
        log_id = replayed[-1]["log_id"]
        first.close()

        ext.write_message(json.dumps({"kind": "event", "name": "cell_deleted", "data": {"cell_id": "a"}}))

        returning = await websocket_connect(url)
        returning.write_message(
            json.dumps(
                {
                    "kind": "hello",
                    "protocol": PROTOCOL_VERSION,
                    "role": "mcp",
                    "notebook": "replay.ipynb",
                    "last_event_seq": replayed[-1]["seq"],
                    "log_id": log_id,
                }
            )
        )
        assert (await _recv(returning))["state"] == "joined"
        missed = await _recv(returning)
        assert missed["name"] == "cell_deleted"
        assert missed["seq"] == 3

        ext.close()
        returning.close()

    asyncio.run(scenario())


def test_capabilities_flow_through_real_sockets(relay_url):
    async def scenario():
        url = f"{relay_url}?token={TOKEN}"
        ext = await websocket_connect(url)
        ext.write_message(
            json.dumps(
                {
                    "kind": "hello",
                    "protocol": PROTOCOL_VERSION,
                    "role": "extension",
                    "notebook": "caps.ipynb",
                    "capabilities": ["snapshot", "inspect"],
                }
            )
        )
        mcp = await websocket_connect(url)
        mcp.write_message(hello("mcp", "caps.ipynb"))
        joined = await _recv(mcp)
        assert joined["state"] == "joined"
        assert joined["capabilities"] == ["snapshot", "inspect"]
        ext.close()
        mcp.close()

    asyncio.run(scenario())


def test_unauthenticated_connection_rejected(relay_url):
    async def scenario():
        with pytest.raises(HTTPClientError) as exc_info:
            await websocket_connect(relay_url)
        assert exc_info.value.code == 403

    asyncio.run(scenario())
