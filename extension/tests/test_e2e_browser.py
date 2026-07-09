import asyncio
import itertools
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest
from conftest import free_port, wait_until_up
from tornado.websocket import websocket_connect

from nbclassic_mcp_bridge.switchboard import PROTOCOL_VERSION


_pw = pytest.importorskip("playwright.async_api")
async_playwright = _pw.async_playwright

pytestmark = pytest.mark.e2e

TOKEN = "e2e-test-token"
NOTEBOOK = "test.ipynb"

_SEED_NOTEBOOK = {
    "cells": [
        {
            "cell_type": "code",
            "source": "print('seed one')",
            "metadata": {},
            "outputs": [],
            "execution_count": None,
            "id": "seed1",
        },
        {
            "cell_type": "code",
            "source": "print('seed two')",
            "metadata": {},
            "outputs": [],
            "execution_count": None,
            "id": "seed2",
        },
    ],
    "metadata": {},
    "nbformat": 4,
    "nbformat_minor": 5,
}


def _wait_for_kernel(port, timeout=45):
    """Block until the notebook's kernel exists and is responsive."""
    deadline = time.time() + timeout
    url = f"http://localhost:{port}/api/kernels?token={TOKEN}"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                kernels = json.loads(r.read())
            if any(k.get("execution_state") in ("idle", "busy") for k in kernels):
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError("no kernel became ready")


@pytest.fixture(scope="module")
def nbclassic_port():
    """Run a real nbclassic server with the bridge extension; yield its port."""
    port = free_port()
    nbdir = tempfile.mkdtemp()
    Path(nbdir, NOTEBOOK).write_text(json.dumps(_SEED_NOTEBOOK))
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "jupyter",
            "nbclassic",
            "--port",
            str(port),
            "--no-browser",
            "--ServerApp.jpserver_extensions={'nbclassic_mcp_bridge':True}",
        ],
        cwd=nbdir,
        env={**os.environ, "JUPYTER_TOKEN": TOKEN},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_until_up(port, TOKEN, timeout=40, label="nbclassic")
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _extension_joined(frame):
    return frame.get("kind") == "status" and frame.get("peer") == "extension" and frame.get("state") == "joined"


class McpPeer:
    """The MCP-side peer of the relay -- lets the test issue cmds and read events."""

    def __init__(self, ws):
        self._ws = ws
        self._ids = itertools.count(1)

    @classmethod
    async def connect(cls, port):
        ws = await websocket_connect(f"ws://localhost:{port}/mcp-bridge?token={TOKEN}")
        ws.write_message(
            json.dumps(
                {
                    "kind": "hello",
                    "protocol": PROTOCOL_VERSION,
                    "role": "mcp",
                    "notebook": NOTEBOOK,
                }
            )
        )
        return cls(ws)

    async def _recv(self, timeout):
        raw = await asyncio.wait_for(self._ws.read_message(), timeout=timeout)
        if raw is None:
            raise ConnectionError("relay closed the connection")
        return json.loads(raw)

    async def recv_until(self, predicate, timeout=45):
        """Read frames until ``predicate`` matches, discarding the rest."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError("expected frame did not arrive")
            frame = await self._recv(remaining)
            if predicate(frame):
                return frame

    async def command(self, op, args, timeout=30):
        msg_id = next(self._ids)
        self._ws.write_message(json.dumps({"kind": "cmd", "id": msg_id, "op": op, "args": args}))
        reply = await self.recv_until(lambda f: f.get("kind") == "reply" and f.get("id") == msg_id, timeout)
        assert reply.get("ok"), reply.get("error")
        return reply.get("result")

    def close(self):
        self._ws.close()


def test_mcp_commands_drive_the_live_notebook(nbclassic_port):
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page()
            mcp = await McpPeer.connect(nbclassic_port)
            try:
                url = f"http://localhost:{nbclassic_port}/notebooks/{NOTEBOOK}?token={TOKEN}"
                await page.goto(url)
                # main.js connecting is what makes the relay announce the extension peer.
                await mcp.recv_until(_extension_joined)

                cells = await mcp.command("snapshot", {})
                assert [c["source"] for c in cells] == ["print('seed one')", "print('seed two')"]

                created = await mcp.command(
                    "insert_cell",
                    {"index": 0, "cell_type": "code", "source": "print('inserted')"},
                )
                texts = await page.evaluate("Jupyter.notebook.get_cells().map(c => c.get_text())")
                assert texts[0] == "print('inserted')"
                assert len(texts) == 3

                await mcp.command("set_source", {"cell_id": cells[0]["cell_id"], "source": "print('edited')"})
                edited = await page.evaluate(
                    "(id) => Jupyter.notebook.get_cells().find(c => c.id === id).get_text()",
                    cells[0]["cell_id"],
                )
                assert edited == "print('edited')"

                _wait_for_kernel(nbclassic_port)
                await page.wait_for_function(
                    "() => { var k = window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel;"
                    " return !!k && (!k.is_connected || k.is_connected()); }",
                    timeout=45000,
                )
                result = await mcp.command("execute_cell", {"cell_id": created["cell_id"]}, timeout=60)
                stdout = "".join(o.get("text", "") for o in result["outputs"] if o.get("output_type") == "stream")
                assert "inserted" in stdout
            finally:
                mcp.close()
                await browser.close()

    asyncio.run(scenario())


def test_human_edits_surface_as_events(nbclassic_port):
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page()
            mcp = await McpPeer.connect(nbclassic_port)
            try:
                url = f"http://localhost:{nbclassic_port}/notebooks/{NOTEBOOK}?token={TOKEN}"
                await page.goto(url)
                await mcp.recv_until(_extension_joined)

                # Edit a cell as a human would, then leave edit mode to flush.
                await page.locator(".cell .CodeMirror").first.click()
                await page.keyboard.type(" # typed by a human")
                await page.keyboard.press("Escape")

                event = await mcp.recv_until(lambda f: f.get("kind") == "event" and f.get("name") == "source_changed")
                assert "typed by a human" in event["data"]["source"]
            finally:
                mcp.close()
                await browser.close()

    asyncio.run(scenario())
