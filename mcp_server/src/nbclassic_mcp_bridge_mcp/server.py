import asyncio
import base64
import hashlib
import logging
import os
import sys
from copy import deepcopy
from pathlib import Path
from typing import Literal, TypedDict

from mcp.server.fastmcp import FastMCP, Image

from nbclassic_mcp_bridge_mcp import discovery
from nbclassic_mcp_bridge_mcp.registry import Attachment, NotebookRegistry
from nbclassic_mcp_bridge_mcp.relay_client import RelayClient


log = logging.getLogger(__name__)

mcp = FastMCP("nbclassic-mcp-bridge")

# Configured like the upstream jupyter-mcp-server: a Jupyter URL + token.
_JUPYTER_URL = os.environ.get("JUPYTER_URL", "http://localhost:8888")
_registry = NotebookRegistry(jupyter_url=_JUPYTER_URL, token=os.environ.get("JUPYTER_TOKEN", ""))

# Longest text kept verbatim, per output payload and per cell source. Anything longer is truncated so
# one runaway cell or output cannot blow the response budget; source gets the larger allowance because
# it is the content worth reading.
_OUTPUT_CHAR_LIMIT = 4096
_SOURCE_CHAR_LIMIT = 16384

# How long a newly opened tab has to reach the relay, and how often the rooms endpoint is checked
# while waiting. A tab that takes longer is reported as not yet arrived rather than waited on: the
# attachment is the only thing missing, and use_notebook completes it whenever the human is ready.
_TAB_ARRIVAL_TIMEOUT_S = 15.0
_ROOM_POLL_INTERVAL_S = 0.25


class SeedCell(TypedDict):
    """One cell of a notebook being created, as ``create_notebook`` accepts it."""

    cell_type: Literal["code", "markdown", "raw"]
    source: str


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean environment variable, falling back to ``default``."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# Image outputs (base64 PNGs and the like) are large and burn tokens, so they are dropped from text
# results unless ALLOW_IMG_OUTPUT is set; an output's text/plain representation is always kept.
ALLOW_IMG_OUTPUT = _env_flag("ALLOW_IMG_OUTPUT", False)


def _as_text(value) -> str:
    """Join an nbformat multiline string (str or list of str) into one string."""
    return "".join(value) if isinstance(value, list) else str(value)


def _truncate(text: str, limit: int = _OUTPUT_CHAR_LIMIT) -> str:
    """Cap ``text`` at ``limit`` characters, marking how much was dropped."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated -- {len(text) - limit} more chars]"


def _clean_output(output, truncate: bool = True) -> None:
    """Strip images, and (when ``truncate``) cap long text, in one output."""
    if not isinstance(output, dict):
        return
    if truncate and "text" in output:  # stream output
        output["text"] = _truncate(_as_text(output["text"]))
    if truncate and isinstance(output.get("traceback"), list):  # error output
        output["traceback"] = [_truncate(_as_text(output["traceback"]))]
    data = output.get("data")
    if isinstance(data, dict):  # display_data / execute_result
        for mime, payload in list(data.items()):
            if mime.startswith("image/"):
                if not ALLOW_IMG_OUTPUT:
                    data[mime] = f"<{mime} omitted; set ALLOW_IMG_OUTPUT=1 to include>"
            elif truncate and isinstance(payload, (str, list)):
                data[mime] = _truncate(_as_text(payload))


def _clean_event_outputs(payload: dict) -> dict:
    """Clean outputs carried by cell_executed events (poll_events)."""
    for event in payload.get("events", []):
        for output in event.get("data", {}).get("outputs", []):
            _clean_output(output)
    return payload


def _summarize_output(output: dict) -> dict:
    """Describe one output compactly, without its payload."""
    kind = output.get("output_type")
    if kind == "stream":
        text = _as_text(output.get("text", ""))
        return {"output_type": kind, "name": output.get("name"), "chars": len(text)}
    if kind == "error":
        return {"output_type": kind, "ename": output.get("ename"), "evalue": output.get("evalue")}
    if kind in ("display_data", "execute_result"):
        return {"output_type": kind, "mime_types": sorted(output.get("data", {}))}
    return {"output_type": kind}


def _outline_cell(cell: dict) -> dict:
    """Reduce a cell to truncated source plus a compact output summary.

    Prefer an extension-side ``output_summary`` when present; otherwise summarize the full outputs here.
    """
    summary = cell.get("output_summary")
    if summary is None:
        summary = [_summarize_output(o) for o in cell.get("outputs", [])]
    return {
        "cell_id": cell.get("cell_id"),
        "index": cell.get("index"),
        "cell_type": cell.get("cell_type"),
        "source": _truncate(_as_text(cell.get("source", "")), _SOURCE_CHAR_LIMIT),
        "output_summary": summary,
    }


def _cell_source_view(cell: dict, full: bool) -> dict:
    """Project a cell to its source plus id/index/type, dropping outputs."""
    source = _as_text(cell.get("source", ""))
    return {
        "cell_id": cell.get("cell_id"),
        "index": cell.get("index"),
        "cell_type": cell.get("cell_type"),
        "source": source if full else _truncate(source, _SOURCE_CHAR_LIMIT),
    }


def _cell_output_view(cell: dict, full: bool) -> dict:
    """Project a cell to its outputs plus id, dropping source."""
    outputs = cell.get("outputs", [])
    for output in outputs:
        _clean_output(output, truncate=not full)
    return {"cell_id": cell.get("cell_id"), "outputs": outputs}


def _extract_images(cell: dict) -> list[tuple[str, str]]:
    """Collect a cell's raster image payloads as (mime type, base64) pairs.

    Walks the outputs in document order. SVG is skipped -- it is text, not base64, and is delivered
    through ``read_cell_output`` like any other text payload.
    """
    images = []
    for output in cell.get("outputs", []):
        data = output.get("data") if isinstance(output, dict) else None
        if not isinstance(data, dict):
            continue
        images.extend(
            (mime, _as_text(data[mime]))
            for mime in sorted(data)
            if mime.startswith("image/") and mime != "image/svg+xml"
        )
    return images


def _derive_endpoint(project_path: str) -> tuple[str, str]:
    """Derive a project's Jupyter URL and token from its directory path.

    The token is the full sha256 hex digest of the physical (symlink-resolved) project path; the
    URL is the configured ``JUPYTER_URL``.
    """
    pwd = Path(project_path).expanduser().resolve()
    token = hashlib.sha256(str(pwd).encode()).hexdigest()
    return _JUPYTER_URL, token


def _find_attachment(notebook: str | None) -> Attachment:
    """Resolve a tool's ``notebook`` argument, falling back to the current notebook when omitted.

    Match a name loosely against the attached notebooks. Raise RuntimeError when it matches none or
    several -- a tool must never act on a guess.
    """
    if notebook is None:
        return _registry.current_attachment()
    matches = _registry.find(notebook)
    if not matches:
        raise _registry.not_attached_error(notebook)
    if len(matches) > 1:
        listing = ", ".join(_registry.label(match) for match in matches)
        raise RuntimeError(f"'{notebook}' matches several attached notebooks ({listing}); name one exactly")
    return matches[0]


def _find_event_source(notebook: str) -> Attachment:
    """Resolve a poll filter against every notebook the retained events came from, attached or not.

    A detached notebook keeps the events it already raised, so naming it here keeps working.
    """
    matches = _registry.find_source(notebook)
    if not matches:
        raise RuntimeError(f"no retained events from '{notebook}'; attached: {_registry.attached_listing()}")
    if len(matches) > 1:
        listing = ", ".join(_registry.label(match) for match in matches)
        raise RuntimeError(f"'{notebook}' matches several notebooks ({listing}); name one exactly")
    return matches[0]


def _resolve(notebook: str | None) -> tuple[RelayClient, str]:
    """Return the client to read from and the name to report, leaving the current notebook alone."""
    attachment = _find_attachment(notebook)
    return _registry.client_for(attachment), _registry.label(attachment)


def _target(notebook: str | None) -> tuple[RelayClient, str]:
    """Return the client to act on and the name to report, making it the current notebook.

    Changing or executing a notebook says the assistant is working there; merely reading one does
    not, so reads go through ``_resolve`` and leave the default where it was.
    """
    attachment = _find_attachment(notebook)
    return _registry.make_current(attachment), _registry.label(attachment)


def _echo(result: dict | None, notebook: str) -> dict:
    """Stamp the notebook a tool acted on into its result, so a mistarget shows up in the reply."""
    return {**(result or {}), "notebook": notebook}


async def _open_notebooks(jupyter_url: str, token: str) -> list[dict]:
    """Fetch the merged discovery view for one Jupyter server."""
    sessions = await discovery.list_sessions(jupyter_url, token)
    rooms = await discovery.fetch_rooms(jupyter_url, token)
    return discovery.merge_notebook_view(sessions, rooms)


@mcp.tool()
async def list_notebooks() -> list[dict]:
    """List the notebooks open on every Jupyter server this session has touched.

    Each record carries the notebook's path, kernel state, whether a browser tab and an assistant
    are connected, whether the bridge is ``attached`` to it, and whether it is the ``current``
    notebook that commands target by default. ``jupyter_url`` appears only when more than one
    server is in play. A server that cannot be reached contributes one ``{jupyter_url, error}``
    record instead of failing the whole listing, so the notebooks on the reachable servers stay
    visible.
    """
    endpoints = _registry.endpoints()
    records = []
    for url, token in endpoints:
        try:
            open_here = await _open_notebooks(url, token)
        except RuntimeError as exc:
            records.append({"jupyter_url": url, "error": str(exc)})
            continue
        for record in open_here:
            attachment = Attachment(url, record["path"])
            record["attached"] = _registry.is_attached(attachment)
            record["current"] = _registry.is_current(attachment)
            if len(endpoints) > 1:
                record["jupyter_url"] = url
            records.append(record)
    return records


@mcp.tool()
async def use_notebook(path: str | None = None) -> str:
    """Attach the bridge to a notebook open in the classic Notebook UI, and make it current.

    Notebooks already attached stay attached, so several can be held at once; this one becomes the
    notebook commands target by default. With no ``path``, attach to the only open notebook
    (preferring one with a connected browser tab); when several are open, list them instead of
    guessing. A ``path`` that does not exactly match an open notebook is resolved fuzzily
    (case-insensitive, basename, substring). The reply states whether the notebook's browser tab is
    connected -- commands only work once the human has the notebook open. Default None.
    """
    notebooks = await _open_notebooks(_registry.jupyter_url, _registry.token)
    note = ""
    if path is None:
        candidates = [n for n in notebooks if n["browser_tab_connected"]] or notebooks
        if not candidates:
            return f"no notebooks are open on {_registry.jupyter_url}; open one in the classic UI first"
        if len(candidates) > 1:
            listing = ", ".join(n["path"] for n in candidates)
            return f"several notebooks are open ({listing}); call use_notebook with one of these paths"
        path = candidates[0]["path"]
    else:
        match, ambiguous = discovery.match_notebook(path, [n["path"] for n in notebooks])
        if match is None and ambiguous:
            return f"'{path}' matches several open notebooks: {', '.join(ambiguous)}; pick one"
        if match is not None and match != path:
            note = f" (resolved from '{path}')"
            path = match
        # No match at all still attaches verbatim: the room outlives this call, so opening the
        # notebook afterwards completes the attachment.
    client = await _registry.attach(path)
    others = sorted(
        _registry.label(attachment) for attachment in _registry.attachments() if not _registry.is_current(attachment)
    )
    also = f"; also attached: {', '.join(others)}" if others else ""
    if await client.extension_present():
        return f"attached to {path}{note} (browser tab connected){also}"
    open_listing = ", ".join(n["path"] for n in notebooks) or "none"
    return (
        f"attached to {path}{note}, but no browser tab is connected for that path -- "
        f"commands will fail until the notebook is open in the classic UI. "
        f"Open notebooks: {open_listing}{also}"
    )


@mcp.tool()
async def use_server(jupyter_url: str, token: str) -> str:
    """Point new attachments at a different Jupyter server.

    Notebooks already attached stay attached to the server they were attached against, so notebooks
    on two servers can be held at once; follow with use_notebook to attach one here.
    """
    _registry.retarget(jupyter_url, token)
    return f"new attachments will use {jupyter_url}"


@mcp.tool()
async def use_project(path: str) -> str:
    """Point new attachments at the Jupyter server launched for a project directory.

    Supports the convention where a project-local server's token is the sha256 hex digest of the
    project's resolved path. Notebooks already attached stay attached to their own server; follow
    with use_notebook to attach one here.
    """
    jupyter_url, token = _derive_endpoint(path)
    _registry.retarget(jupyter_url, token)
    return f"new attachments will use {jupyter_url} (derived from {path})"


async def _attach_when_tab_arrives(path: str, timeout: float) -> RelayClient | None:
    """Attach to ``path`` once a browser tab for it joins the relay; return None if none arrives.

    Parameters
    ----------
    path : str
        Server-relative path of the notebook whose tab is expected.
    timeout : float
        Seconds to wait for the tab before giving up.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        rooms = await discovery.fetch_rooms(_registry.jupyter_url, _registry.token)
        if "extension" in rooms.get(path, []):
            return await _registry.attach(path)
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(_ROOM_POLL_INTERVAL_S)


async def _tab_that_can_open() -> RelayClient | None:
    """Return an attached tab on the default server that can be asked to open another notebook.

    Prefer the current notebook's tab, since that is the window the human is looking at. Tabs on
    other servers are no use: a tab can only open a notebook its own server serves.
    """
    on_default = [
        attachment for attachment in _registry.attachments() if attachment.jupyter_url == _registry.jupyter_url
    ]
    try:
        current = _registry.current_attachment()
    except RuntimeError:
        current = None
    ordered = [attachment for attachment in on_default if attachment == current]
    ordered += [attachment for attachment in on_default if attachment != current]
    for attachment in ordered:
        client = _registry.client_for(attachment)
        if await client.extension_present():
            return client
    return None


async def _open_and_attach(created: str) -> str:
    """Ask a tab to open the notebook at ``created``, attach once its tab arrives, and report back.

    Every failure names the notebook's URL so the human can open it by hand, because the file
    exists from here on and only the attachment is missing.
    """
    url = discovery.notebook_url(_registry.jupyter_url, created)
    by_hand = f"open {url} yourself, then call use_notebook to attach"
    opener = await _tab_that_can_open()
    if opener is None:
        return f"created {created}, but no attached tab could be asked to open it; {by_hand}"
    try:
        result = await opener.command("open_notebook", {"path": created})
    except RuntimeError as exc:
        return f"created {created}, but the tab could not open it ({exc}); {by_hand}"
    # A peer that replies without a result payload leaves command() returning None.
    result = result or {}
    if not result.get("opened"):
        reason = result.get("reason", "no reason given")
        return f"created {created}, but the browser refused the new tab ({reason}); {by_hand}"
    if await _attach_when_tab_arrives(created, _TAB_ARRIVAL_TIMEOUT_S) is None:
        return (
            f"created {created} and a tab was opened for it, but that tab has not reached the bridge "
            f"within {_TAB_ARRIVAL_TIMEOUT_S:.0f}s; call use_notebook once it has finished loading"
        )
    return f"created {created}, opened it in a new tab, and attached; it is now the current notebook"


@mcp.tool()
async def create_notebook(
    path: str, cells: list[SeedCell] | None = None, kernel_name: str | None = None, open: bool = False
) -> str:
    """Create a new notebook file on the Jupyter server, optionally seeded with cells.

    With ``open`` set, ask an attached browser tab to open the new notebook in a tab of its own and
    attach to it, leaving it the current notebook; the reply says plainly when that could not be
    done and what to open by hand. Otherwise the file is written and nothing else happens, so
    follow up by asking the human to open it and then calling use_notebook. ``path`` must end in
    .ipynb, is not created if anything is already there, and its parent directories must already
    exist. ``cells`` are ``{cell_type, source}`` mappings in order; ``kernel_name`` records a kernel
    in the notebook's metadata, and omitting it lets Jupyter pick its default. Default cells None,
    kernel_name None, open False.
    """
    record = await discovery.create_notebook(_registry.jupyter_url, _registry.token, path, cells, kernel_name)
    created = record.get("path", path)
    if not open:
        return (
            f"created {created} on {_registry.jupyter_url}; open it in the classic UI, then call use_notebook to attach"
        )
    return await _open_and_attach(created)


@mcp.tool()
async def detach_notebook(notebook: str) -> str:
    """Stop tracking a notebook. Its browser tab and kernel are left untouched.

    Drops the bridge's connection only -- nothing is saved, closed, or reverted, and the human's tab
    keeps working. Detaching the current notebook leaves no current notebook, so call use_notebook
    to pick another.
    """
    matches = _registry.find(notebook)
    if not matches:
        raise _registry.not_attached_error(notebook)
    if len(matches) > 1:
        listing = ", ".join(_registry.label(match) for match in matches)
        return f"'{notebook}' matches several attached notebooks ({listing}); name one exactly"
    target = matches[0]
    was_current = _registry.is_current(target)
    await _registry.detach(target)
    suffix = "; no current notebook -- call use_notebook to pick one" if was_current else ""
    return f"detached {notebook}; still attached: {_registry.attached_listing()}{suffix}"


@mcp.tool()
async def read_notebook(notebook: str | None = None) -> list[dict]:
    """List every cell: id, index, type, source, and an output summary.

    Source longer than ~16k chars is truncated; outputs are summarized, not included -- call
    ``read_cell_source`` / ``read_cell_output`` for one cell. Act on the current notebook unless
    ``notebook`` names another attached one.
    """
    client, _ = _resolve(notebook)
    cells = await client.command("snapshot", {"outputs": "summary"})
    return [_outline_cell(c) for c in cells]


@mcp.tool()
async def read_cell_source(cell_id: str, full: bool = False, notebook: str | None = None) -> dict:
    """Return one cell's source (with its id, index, and type), not its outputs.

    Source longer than ~16k chars is truncated unless ``full`` is true. Act on the current notebook
    unless ``notebook`` names another attached one.
    """
    client, _ = _resolve(notebook)
    return _cell_source_view(await client.command("read_cell", {"cell_id": cell_id}), full)


@mcp.tool()
async def read_cell_output(cell_id: str, full: bool = False, notebook: str | None = None) -> dict:
    """Return one cell's outputs (with its id), not its source.

    Long text in each output is truncated unless ``full`` is true. Image payloads are stubbed out
    (unless ALLOW_IMG_OUTPUT is set); to look at one, call ``read_cell_image``. Act on the current
    notebook unless ``notebook`` names another attached one.
    """
    client, _ = _resolve(notebook)
    return _cell_output_view(await client.command("read_cell", {"cell_id": cell_id}), full)


@mcp.tool()
async def read_cell_image(cell_id: str, image_index: int = 0, notebook: str | None = None) -> Image:
    """Return one of a cell's image outputs as a viewable image.

    A cell can hold several images; ``image_index`` picks among them in document order. Default 0
    (the first). Use this to actually look at a plot -- image payloads are stubbed out of every
    text-returning tool. Act on the current notebook unless ``notebook`` names another attached one.
    """
    client, _ = _resolve(notebook)
    cell = await client.command("read_cell", {"cell_id": cell_id})
    images = _extract_images(cell)
    if not images:
        raise RuntimeError(f"cell {cell_id} has no raster image outputs")
    if not 0 <= image_index < len(images):
        raise RuntimeError(
            f"cell {cell_id} has {len(images)} image output(s); image_index {image_index} is out of range"
        )
    mime, payload = images[image_index]
    return Image(data=base64.b64decode(payload), format=mime.removeprefix("image/"))


@mcp.tool()
async def insert_cell(index: int, cell_type: str, source: str, notebook: str | None = None) -> dict:
    """Insert a new cell at ``index``.

    Act on the current notebook unless ``notebook`` names another attached one.
    """
    client, target = _target(notebook)
    args = {"index": index, "cell_type": cell_type, "source": source}
    return _echo(await client.command("insert_cell", args), target)


@mcp.tool()
async def set_cell_source(cell_id: str, source: str, notebook: str | None = None) -> dict:
    """Replace a cell's source.

    Returns ``{"cell_id": ..., "status": "written"}`` on success. If the human currently has the
    target cell focused, the bridge skips the write to avoid clobbering an in-progress edit and
    returns ``{"cell_id": ..., "status": "skipped", "reason": "focused"}``, so check ``status``
    before assuming the write took effect. Act on the current notebook unless ``notebook`` names
    another attached one.
    """
    client, target = _target(notebook)
    return _echo(await client.command("set_source", {"cell_id": cell_id, "source": source}), target)


@mcp.tool()
async def execute_cell(cell_id: str, timeout_s: float = 120, notebook: str | None = None) -> dict:
    """Execute a cell in the live UI and return its outputs (long text truncated).

    Raise a timeout error if the cell is still running after ``timeout_s`` seconds (the cell itself
    keeps running). Default 120. Act on the current notebook unless ``notebook`` names another
    attached one.
    """
    client, target = _target(notebook)
    args = {"cell_id": cell_id, "timeout_ms": int(timeout_s * 1000)}
    return _echo(_cell_output_view(await client.command("execute_cell", args), full=False), target)


@mcp.tool()
async def run_cells(cell_ids: list[str], timeout_s: float = 600, notebook: str | None = None) -> dict:
    """Execute several cells with one call, returning per-cell outputs in the requested order.

    Executions queue in the kernel like the notebook's Run All, so an error in one cell aborts the
    ones after it. On timeout, finished cells keep their outputs and unfinished ones are marked
    "timed out" (they keep running in the kernel). Long text is truncated. Default timeout 600. Act
    on the current notebook unless ``notebook`` names another attached one.
    """
    client, target = _target(notebook)
    result = await client.command("run_cells", {"cell_ids": cell_ids, "timeout_ms": int(timeout_s * 1000)})
    for cell_result in result.get("results", []):
        for output in cell_result.get("outputs", []):
            _clean_output(output)
    return _echo(result, target)


@mcp.tool()
async def inspect_kernel(code: str, timeout_s: float = 30, full: bool = False, notebook: str | None = None) -> dict:
    """Evaluate code in the notebook's live kernel without touching cells, outputs, or history.

    Nothing appears in the notebook and the In[]/Out[] counters are unchanged, so use this to look
    at state -- ``df.shape``, ``data.columns``, ``locals().keys()`` -- rather than to do work the
    human should see. Returns ``{status, outputs}``; long text is truncated unless ``full`` is
    true. Default timeout 30. Act on the current notebook unless ``notebook`` names another
    attached one.
    """
    client, _ = _resolve(notebook)
    result = await client.command("inspect", {"code": code, "timeout_ms": int(timeout_s * 1000)})
    for output in result.get("outputs", []):
        _clean_output(output, truncate=not full)
    return result


@mcp.tool()
async def interrupt_kernel(notebook: str | None = None) -> dict:
    """Send a KeyboardInterrupt to the notebook's kernel.

    Stops whatever is currently running -- including a cell the human started -- so use it to stop
    a runaway execution you triggered. Act on the current notebook unless ``notebook`` names another
    attached one.
    """
    client, _ = _target(notebook)
    return await client.command("interrupt_kernel", {})


@mcp.tool()
async def kernel_status(notebook: str | None = None) -> dict:
    """Report the kernel's last known lifecycle state, connectivity, and name.

    ``execute_cell`` and ``inspect_kernel`` fail immediately with "kernel is not connected" while
    the kernel is dead or restarting; poll here (or watch kernel_status events) before retrying. The
    result names the notebook it describes. Act on the current notebook unless ``notebook`` names
    another attached one.
    """
    client, target = _resolve(notebook)
    return _echo(await client.command("kernel_info", {}), target)


@mcp.tool()
async def delete_cell(cell_id: str, notebook: str | None = None) -> dict:
    """Delete a cell by id.

    Act on the current notebook unless ``notebook`` names another attached one.
    """
    client, target = _target(notebook)
    return _echo(await client.command("delete_cell", {"cell_id": cell_id}), target)


@mcp.tool()
async def move_cell(cell_id: str, index: int, notebook: str | None = None) -> dict:
    """Move a cell to a new index in the notebook.

    Act on the current notebook unless ``notebook`` names another attached one.
    """
    client, target = _target(notebook)
    return _echo(await client.command("move_cell", {"cell_id": cell_id, "index": index}), target)


@mcp.tool()
async def undo_last_change(notebook: str | None = None) -> dict:
    """Undo your most recent notebook mutation (set/insert/delete/move; executions are not undoable).

    An entry is skipped rather than applied when the human has touched that cell since, so undo can
    never destroy human work; skipped or not, the entry is consumed. Returns what was undone or why
    it was skipped. The undo stack is per notebook. Act on the current notebook unless ``notebook``
    names another attached one.
    """
    client, target = _target(notebook)
    return _echo(await client.command("undo_last", {}), target)


@mcp.tool()
async def undo_all_changes(notebook: str | None = None) -> dict:
    """Undo every recorded mutation of yours, newest first, with per-entry results.

    Entries the human has since touched are skipped, not applied (and consumed either way);
    executions are not undoable. Undoes one notebook's stack: act on the current notebook unless
    ``notebook`` names another attached one.
    """
    client, target = _target(notebook)
    return _echo(await client.command("undo_all", {}), target)


@mcp.tool()
async def checkpoint_notebook(notebook: str | None = None) -> dict:
    """Checkpoint a notebook's saved file as a restore point.

    Uses Jupyter's single default checkpoint slot -- the same one the human's "Save and
    Checkpoint" writes -- so create one deliberately, not routinely. Act on the current notebook
    unless ``notebook`` names another attached one.
    """
    client, target = _target(notebook)
    record = await discovery.create_checkpoint(client.jupyter_url, client.token, client.notebook)
    return _echo(record, target)


@mcp.tool()
async def restore_notebook_checkpoint(notebook: str | None = None) -> str:
    """Revert a notebook to its checkpoint and reload it in the browser.

    DESTRUCTIVE: the file on disk reverts to the checkpoint and the browser tab reloads from disk,
    discarding any unsaved edits (the human's included). Confirm with the human first. Act on the
    current notebook unless ``notebook`` names another attached one.
    """
    client, _ = _target(notebook)
    url, token, path = client.jupyter_url, client.token, client.notebook
    checkpoints = await discovery.list_checkpoints(url, token, path)
    if not checkpoints:
        raise RuntimeError(f"no checkpoint exists for {path}; call checkpoint_notebook first")
    await discovery.restore_checkpoint(url, token, path, checkpoints[0]["id"])
    await client.command("reload_notebook", {})
    return f"restored {path} to checkpoint {checkpoints[0]['id']} and reloaded the browser tab"


@mcp.tool()
async def poll_events(cursor: int = 0, notebook: str | None = None) -> dict:
    """Return the human's notebook edits since ``cursor``, across every attached notebook.

    Pass 0 on the first call, then feed the returned ``cursor`` back to get only newer events. Each
    event is ``{notebook, name, data}`` -- one of cell_created, cell_deleted, cell_moved,
    cell_executed, source_changed, focus_changed, bridge_paused, bridge_resumed, kernel_status --
    and reflects the human's actions (never an echo of your own commands) plus kernel failures and
    recoveries. One stream carries every attached notebook in the order things happened, so watching
    several costs one call; ``notebook`` narrows what comes back without changing the cursor, which
    always tracks the whole stream, and may name a notebook that has since been detached. A
    ``dropped`` count appears when the log aged past ``cursor`` before you polled -- that many events
    are gone, and the retained budget is shared across every attached notebook. Events that fire
    while this server is detached are replayed on reconnect from a bounded relay buffer, so brief
    gaps do not lose edits. While the human has the bridge paused, every command fails with "bridge
    paused by the user" until a bridge_resumed event arrives. Image payloads are omitted from
    cell_executed outputs; use ``read_cell_image`` to view them. Default cursor 0, notebook None.
    """
    wanted = _find_event_source(notebook) if notebook is not None else None
    recorded, new_cursor = _registry.events_since(cursor, wanted)
    # Deep-copied because the cleaners mutate outputs in place and these events stay in the log:
    # truncating an already-truncated payload rewrites the marker to understate what was dropped.
    events = [{**deepcopy(event), "notebook": _registry.label(attachment)} for attachment, event in recorded]
    result = {"events": events, "cursor": new_cursor}
    dropped = _registry.dropped_before(cursor)
    if dropped:
        result["dropped"] = dropped
    return _clean_event_outputs(result)


def main():
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info("nbclassic-mcp-bridge MCP server starting; relay target %s", _JUPYTER_URL)
    mcp.run()


if __name__ == "__main__":
    main()
