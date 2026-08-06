import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest


pytest.importorskip("playwright.async_api")
pytest.importorskip("nbclassic_mcp_bridge")
# The helper that serves the working tree's extension lives with the extension's own tests. Adding
# the path here rather than in pyproject.toml keeps a single-file pytest run working, and adding a
# second conftest.py would collide with the extension tests' `from conftest import ...`.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "extension" / "tests"))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ImageContent
from playwright.async_api import async_playwright
from repo_assets import jupyter_path_serving_the_repo


pytestmark = pytest.mark.e2e

TOKEN = "mcp-e2e-token"
NOTEBOOK = "imaged.ipynb"

# A real 1x1 PNG, stored as a display_data output in the seed file so no kernel is needed.
ONE_PX_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="

_SEED_NOTEBOOK = {
    "cells": [
        {
            "cell_type": "code",
            "source": "show_plot()",
            "metadata": {},
            "execution_count": 1,
            "id": "img1",
            "outputs": [
                {
                    "output_type": "display_data",
                    "data": {"image/png": ONE_PX_PNG, "text/plain": "<Figure>"},
                    "metadata": {},
                }
            ],
        },
    ],
    "metadata": {},
    "nbformat": 4,
    "nbformat_minor": 5,
}


def _spawn_nbclassic(port, nbdir):
    return subprocess.Popen(
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
        env={
            **os.environ,
            "JUPYTER_TOKEN": TOKEN,
            "JUPYTER_PATH": jupyter_path_serving_the_repo(nbdir),
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _free_port():
    listener = socket.socket()
    listener.bind(("", 0))
    port = listener.getsockname()[1]
    listener.close()
    return port


def _wait_until_up(port, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/api/status?token={TOKEN}", timeout=1) as r:
                if r.status == 200:
                    return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError("nbclassic did not come up")


def test_read_cell_image_round_trips_over_the_mcp_protocol(tmp_path):
    """Drive the real server binary over stdio: a stored PNG must arrive as an ImageContent block."""
    port = _free_port()
    Path(tmp_path, NOTEBOOK).write_text(json.dumps(_SEED_NOTEBOOK))
    server_proc = _spawn_nbclassic(port, tmp_path)

    async def scenario():
        _wait_until_up(port)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page()
            await page.goto(f"http://localhost:{port}/notebooks/{NOTEBOOK}?token={TOKEN}")
            await page.wait_for_selector(".cell", timeout=20000)

            bridge = StdioServerParameters(
                command=sys.executable,
                args=["-m", "nbclassic_mcp_bridge_mcp.server"],
                env={
                    **os.environ,
                    "JUPYTER_URL": f"http://localhost:{port}",
                    "JUPYTER_TOKEN": TOKEN,
                },
            )
            async with stdio_client(bridge) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()

                    attached = await session.call_tool("use_notebook", {})
                    attach_message = attached.content[0].text
                    assert f"attached to {NOTEBOOK}" in attach_message
                    assert "browser tab connected" in attach_message

                    listing = await session.call_tool("list_notebooks", {})
                    listing_text = " ".join(part.text for part in listing.content if hasattr(part, "text"))
                    assert NOTEBOOK in listing_text
                    assert '"browser_tab_connected": true' in listing_text

                    image_result = await session.call_tool("read_cell_image", {"cell_id": "img1"})
                    image = image_result.content[0]
                    assert isinstance(image, ImageContent)
                    assert image.mimeType == "image/png"
                    assert base64.b64decode(image.data) == base64.b64decode(ONE_PX_PNG)

            await browser.close()

    try:
        asyncio.run(asyncio.wait_for(scenario(), timeout=180))
    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server_proc.kill()


def test_checkpoint_and_restore_revert_the_notebook(tmp_path):
    port = _free_port()
    Path(tmp_path, NOTEBOOK).write_text(json.dumps(_SEED_NOTEBOOK))
    server_proc = _spawn_nbclassic(port, tmp_path)

    def put_source(source):
        notebook = json.loads(Path(tmp_path, NOTEBOOK).read_text())
        notebook["cells"][0]["source"] = source
        body = json.dumps({"type": "notebook", "format": "json", "content": notebook}).encode()
        request = urllib.request.Request(
            f"http://localhost:{port}/api/contents/{NOTEBOOK}?token={TOKEN}",
            data=body,
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(request, timeout=5)

    async def scenario():
        _wait_until_up(port)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page()
            await page.goto(f"http://localhost:{port}/notebooks/{NOTEBOOK}?token={TOKEN}")
            await page.wait_for_selector(".cell", timeout=20000)

            bridge = StdioServerParameters(
                command=sys.executable,
                args=["-m", "nbclassic_mcp_bridge_mcp.server"],
                env={**os.environ, "JUPYTER_URL": f"http://localhost:{port}", "JUPYTER_TOKEN": TOKEN},
            )
            async with stdio_client(bridge) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.call_tool("use_notebook", {})

                    await session.call_tool("checkpoint_notebook", {})
                    put_source("overwritten_on_disk()")

                    restored = await session.call_tool("restore_notebook_checkpoint", {})
                    assert "restored" in restored.content[0].text

                    listing = await session.call_tool("read_notebook", {})
                    listing_text = " ".join(part.text for part in listing.content if hasattr(part, "text"))
                    assert "show_plot()" in listing_text
                    assert "overwritten_on_disk" not in listing_text

            await browser.close()

    try:
        asyncio.run(asyncio.wait_for(scenario(), timeout=180))
    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server_proc.kill()
