import asyncio
import json
import os
import subprocess
import sys

import pytest
from conftest import free_port, wait_until_up
from tornado.httpclient import HTTPClientError
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


def test_unauthenticated_connection_rejected(relay_url):
    async def scenario():
        with pytest.raises(HTTPClientError) as exc_info:
            await websocket_connect(relay_url)
        assert exc_info.value.code == 403

    asyncio.run(scenario())
