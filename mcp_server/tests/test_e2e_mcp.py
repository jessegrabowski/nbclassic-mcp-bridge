import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from contextlib import asynccontextmanager, contextmanager
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


def _serving(tmp_path, *notebooks):
    """Seed ``notebooks`` with the standard fixture and start nbclassic over them.

    Return the server's port and its process.
    """
    port = _free_port()
    for notebook in notebooks:
        Path(tmp_path, notebook).write_text(json.dumps(_SEED_NOTEBOOK))
    return port, _spawn_nbclassic(port, tmp_path)


def _stop(server_proc):
    server_proc.terminate()
    try:
        server_proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server_proc.kill()


@contextmanager
def _nbclassic(tmp_path, *notebooks):
    """Serve ``notebooks`` for the duration of the block, always stopping the server afterwards."""
    port, server_proc = _serving(tmp_path, *notebooks)
    try:
        yield port
    finally:
        _stop(server_proc)


def _driven(scenario, timeout=300):
    """Run one test's async scenario under a wall-clock cap, so a hang fails instead of stalling CI."""
    asyncio.run(asyncio.wait_for(scenario(), timeout=timeout))


async def _open_tabs(browser, port, *notebooks):
    """Open one browser tab per notebook and return them in the order given."""
    pages = []
    for notebook in notebooks:
        page = await browser.new_page()
        await page.goto(f"http://localhost:{port}/notebooks/{notebook}?token={TOKEN}")
        await page.wait_for_selector(".cell", timeout=20000)
        pages.append(page)
    return pages


@asynccontextmanager
async def _bridge_session(port):
    """Run the real MCP server binary over stdio against the Jupyter server on ``port``."""
    bridge = StdioServerParameters(
        command=sys.executable,
        args=["-m", "nbclassic_mcp_bridge_mcp.server"],
        env={**os.environ, "JUPYTER_URL": f"http://localhost:{port}", "JUPYTER_TOKEN": TOKEN},
    )
    async with stdio_client(bridge) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _text(result):
    """Join a tool result's text blocks, which is how tools report both prose and JSON."""
    return " ".join(part.text for part in result.content if hasattr(part, "text"))


def _payload(result):
    """Parse a tool result that returns one structured object."""
    return json.loads(_text(result))


async def _prove_kernel_answers(session, notebook, timeout=60):
    """Block until the notebook's kernel round-trips a trivial evaluation through the bridge.

    A kernel whose socket is connected can still drop an execute sent before its iopub
    subscription is established, so wait on an answer rather than on a connection.
    """
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            answer = _payload(await session.call_tool("inspect_kernel", {"code": "1 + 1", "notebook": notebook}))
            # A failed evaluation still returns a reply, and its traceback can contain any digit, so
            # read the value out of the execute_result rather than searching the whole payload.
            values = [
                output.get("data", {}).get("text/plain", "")
                for output in answer.get("outputs", [])
                if output.get("output_type") == "execute_result"
            ]
            if any(value.strip() == "2" for value in values):
                return
            last_error = answer
        except Exception as exc:  # the kernel is not up yet; the deadline is the real guard
            last_error = str(exc)
        await asyncio.sleep(0.5)
    raise RuntimeError(f"kernel for {notebook} never answered: {last_error}")


async def _drain_source_changes(session, cursor, timeout=30):
    """Poll until at least one source_changed arrives; return those events and the new cursor."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        polled = _payload(await session.call_tool("poll_events", {"cursor": cursor}))
        cursor = polled["cursor"]
        changes = [event for event in polled["events"] if event["name"] == "source_changed"]
        if changes:
            return changes, cursor
        await asyncio.sleep(0.5)
    raise RuntimeError("no source_changed event arrived")


async def _stdout_of(session, cell_id, notebook):
    """Execute a cell and return its stdout, so a test asserts on output rather than on a status."""
    result = await session.call_tool("execute_cell", {"cell_id": cell_id, "notebook": notebook})
    outputs = _payload(result).get("outputs", [])
    return "".join(output.get("text", "") for output in outputs if output.get("output_type") == "stream")


def test_read_cell_image_round_trips_over_the_mcp_protocol(tmp_path):
    """Drive the real server binary over stdio: a stored PNG must arrive as an ImageContent block."""
    with _nbclassic(tmp_path, NOTEBOOK) as port:

        async def scenario():
            _wait_until_up(port)
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                await _open_tabs(browser, port, NOTEBOOK)

                async with _bridge_session(port) as session:
                    attached = await session.call_tool("use_notebook", {})
                    attach_message = attached.content[0].text
                    assert f"attached to {NOTEBOOK}" in attach_message
                    assert "browser tab connected" in attach_message

                    listing = _text(await session.call_tool("list_notebooks", {}))
                    assert NOTEBOOK in listing
                    assert '"browser_tab_connected": true' in listing

                    image_result = await session.call_tool("read_cell_image", {"cell_id": "img1"})
                    image = image_result.content[0]
                    assert isinstance(image, ImageContent)
                    assert image.mimeType == "image/png"
                    assert base64.b64decode(image.data) == base64.b64decode(ONE_PX_PNG)

                await browser.close()

        _driven(scenario, timeout=180)


def test_checkpoint_and_restore_revert_the_notebook(tmp_path):
    with _nbclassic(tmp_path, NOTEBOOK) as port:

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
                await _open_tabs(browser, port, NOTEBOOK)

                async with _bridge_session(port) as session:
                    await session.call_tool("use_notebook", {})

                    await session.call_tool("checkpoint_notebook", {})
                    put_source("overwritten_on_disk()")

                    restored = await session.call_tool("restore_notebook_checkpoint", {})
                    assert "restored" in restored.content[0].text

                    listing = _text(await session.call_tool("read_notebook", {}))
                    assert "show_plot()" in listing
                    assert "overwritten_on_disk" not in listing

                await browser.close()

        _driven(scenario, timeout=180)


FIRST = "first.ipynb"
SECOND = "second.ipynb"


def test_two_notebooks_are_edited_and_executed_independently(tmp_path):
    """Two attached notebooks, two live tabs: each cell's output lands in its own notebook."""
    with _nbclassic(tmp_path, FIRST, SECOND) as port:

        async def scenario():
            _wait_until_up(port)
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                await _open_tabs(browser, port, FIRST, SECOND)

                async with _bridge_session(port) as session:
                    for notebook in (FIRST, SECOND):
                        assert f"attached to {notebook}" in _text(
                            await session.call_tool("use_notebook", {"path": notebook})
                        )
                    await _prove_kernel_answers(session, FIRST)
                    await _prove_kernel_answers(session, SECOND)

                    cells = {}
                    for notebook in (FIRST, SECOND):
                        created = await session.call_tool(
                            "insert_cell",
                            {
                                "index": 0,
                                "cell_type": "code",
                                "source": f"print('from {notebook}')",
                                "notebook": notebook,
                            },
                        )
                        cells[notebook] = _payload(created)["cell_id"]

                    for notebook in (FIRST, SECOND):
                        stdout = await _stdout_of(session, cells[notebook], notebook)
                        assert f"from {notebook}" in stdout

                    # Each notebook must hold its own cell and nothing of the other's.
                    for notebook, other in ((FIRST, SECOND), (SECOND, FIRST)):
                        contents = _text(await session.call_tool("read_notebook", {"notebook": notebook}))
                        assert f"from {notebook}" in contents
                        assert f"from {other}" not in contents

                await browser.close()

        _driven(scenario, timeout=300)


def test_events_from_both_notebooks_merge_into_one_stream(tmp_path):
    """A human edit in each tab reaches one cursor-ordered stream, each event naming its notebook."""
    with _nbclassic(tmp_path, FIRST, SECOND) as port:

        async def scenario():
            _wait_until_up(port)
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                first_tab, second_tab = await _open_tabs(browser, port, FIRST, SECOND)

                async with _bridge_session(port) as session:
                    for notebook in (FIRST, SECOND):
                        await session.call_tool("use_notebook", {"path": notebook})

                    # Human edits, not agent ones: set_text from the page is what the extension debounces
                    # into a source_changed event. The second edit waits for the first event so the
                    # expected order is the one the edits actually happened in.
                    typing = "(text) => Jupyter.notebook.get_cell(0).set_text(text)"
                    await first_tab.evaluate(typing, "# typed in first")
                    first_events, cursor = await _drain_source_changes(session, cursor=0)
                    await second_tab.evaluate(typing, "# typed in second")
                    second_events, cursor = await _drain_source_changes(session, cursor)

                    assert [event["notebook"] for event in first_events] == [FIRST]
                    # Only the second notebook's event comes back, so the cursor advanced past the
                    # first rather than re-delivering it.
                    assert [event["notebook"] for event in second_events] == [SECOND]
                    assert "typed in first" in first_events[0]["data"]["source"]
                    assert "typed in second" in second_events[0]["data"]["source"]

                await browser.close()

        _driven(scenario, timeout=300)
