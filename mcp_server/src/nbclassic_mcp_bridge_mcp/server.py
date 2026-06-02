import hashlib
import logging
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from nbclassic_mcp_bridge_mcp.relay_client import RelayClient


log = logging.getLogger(__name__)

mcp = FastMCP("nbclassic-mcp-bridge")

# Configured like the upstream jupyter-mcp-server: a Jupyter URL + token.
_JUPYTER_URL = os.environ.get("JUPYTER_URL", "http://localhost:8888")
_relay = RelayClient(jupyter_url=_JUPYTER_URL, token=os.environ.get("JUPYTER_TOKEN", ""))

# Longest text kept verbatim, per output payload and per cell source. Anything
# longer is truncated so one runaway cell or output cannot blow the response
# budget. Source gets a larger allowance -- it is the content worth reading.
_OUTPUT_CHAR_LIMIT = 4096
_SOURCE_CHAR_LIMIT = 16384


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean environment variable, falling back to ``default``."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# Image outputs (base64 PNGs and the like) are large and burn tokens, so they
# are dropped from results unless ALLOW_IMG_OUTPUT is set. The text/plain
# representation of an output is always kept.
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
    """Reduce a cell to truncated source plus a compact output summary."""
    return {
        "cell_id": cell.get("cell_id"),
        "index": cell.get("index"),
        "cell_type": cell.get("cell_type"),
        "source": _truncate(_as_text(cell.get("source", "")), _SOURCE_CHAR_LIMIT),
        "output_summary": [_summarize_output(o) for o in cell.get("outputs", [])],
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


def _derive_endpoint(project_path: str) -> tuple[str, str]:
    """Derive a project's Jupyter URL and token from its directory path.

    Mirrors ``jupyter-project-env.sh``: ``sha256`` of the absolute path gives a
    deterministic port (10000-29999) and 32-hex token, so this matches a server
    started with ``launch-nb`` in that directory.
    """
    pwd = os.path.abspath(os.path.expanduser(project_path))
    digest = hashlib.sha256(pwd.encode()).hexdigest()
    port = 10000 + int(digest[:4], 16) % 20000
    token = digest[8:40]
    return f"http://localhost:{port}", token


@mcp.tool()
async def use_notebook(path: str) -> str:
    """Attach the bridge to a notebook open in the classic Notebook UI."""
    await _relay.connect(path)
    return f"attached to {path}"


@mcp.tool()
async def use_server(jupyter_url: str, token: str) -> str:
    """Retarget the bridge at a different Jupyter server, then call use_notebook."""
    await _relay.retarget(jupyter_url, token)
    return f"relay target set to {jupyter_url}"


@mcp.tool()
async def use_project(path: str) -> str:
    """Retarget the bridge at the launch-nb server for a project directory.

    Derives port and token from ``path`` the way jupyter-project-env.sh does;
    follow with use_notebook to attach to a notebook open on that server.
    """
    jupyter_url, token = _derive_endpoint(path)
    await _relay.retarget(jupyter_url, token)
    return f"relay target set to {jupyter_url} (derived from {path})"


@mcp.tool()
async def read_notebook() -> list:
    """List every cell: id, index, type, source, and an output summary.

    Source longer than ~16k chars is truncated; outputs are summarized, not
    included -- call ``read_cell_source`` / ``read_cell_output`` for one cell.
    """
    cells = await _relay.command("snapshot", {})
    return [_outline_cell(c) for c in cells]


@mcp.tool()
async def read_cell_source(cell_id: str, full: bool = False) -> dict:
    """Return one cell's source (with its id, index, and type), not its outputs.

    Source longer than ~16k chars is truncated unless ``full`` is true.
    """
    return _cell_source_view(await _relay.command("read_cell", {"cell_id": cell_id}), full)


@mcp.tool()
async def read_cell_output(cell_id: str, full: bool = False) -> dict:
    """Return one cell's outputs (with its id), not its source.

    Long text in each output is truncated unless ``full`` is true; images are
    governed by ALLOW_IMG_OUTPUT.
    """
    return _cell_output_view(await _relay.command("read_cell", {"cell_id": cell_id}), full)


@mcp.tool()
async def insert_cell(index: int, cell_type: str, source: str) -> dict:
    """Insert a new cell at ``index``."""
    return await _relay.command("insert_cell", {"index": index, "cell_type": cell_type, "source": source})


@mcp.tool()
async def set_cell_source(cell_id: str, source: str) -> dict:
    """Replace a cell's source.

    Returns ``{"cell_id": ..., "status": "written"}`` on success. If the
    human is currently focused on the target cell the write is skipped to
    avoid clobbering an in-progress edit, and the return is
    ``{"cell_id": ..., "status": "skipped", "reason": "focused"}`` — the
    caller should check ``status`` before assuming the write took effect.
    """
    return await _relay.command("set_source", {"cell_id": cell_id, "source": source})


@mcp.tool()
async def execute_cell(cell_id: str) -> dict:
    """Execute a cell in the live UI and return its outputs (long text truncated)."""
    return _cell_output_view(await _relay.command("execute_cell", {"cell_id": cell_id}), full=False)


@mcp.tool()
async def delete_cell(cell_id: str) -> dict:
    """Delete a cell by id."""
    return await _relay.command("delete_cell", {"cell_id": cell_id})


@mcp.tool()
async def move_cell(cell_id: str, index: int) -> dict:
    """Move a cell to a new index in the notebook."""
    return await _relay.command("move_cell", {"cell_id": cell_id, "index": index})


@mcp.tool()
async def poll_events(cursor: int = 0) -> dict:
    """Return the human's notebook edits since ``cursor``.

    Pass 0 on the first call, then feed the returned ``cursor`` back to get only
    newer events. Each event is ``{name, data}`` -- one of cell_created,
    cell_deleted, cell_moved, cell_executed, source_changed, focus_changed.
    """
    events, new_cursor = _relay.events_since(cursor)
    return _clean_event_outputs({"events": events, "cursor": new_cursor})


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
