from collections.abc import Callable, Sequence
from pathlib import PurePosixPath
from urllib.parse import quote
from uuid import uuid4

import httpx


_HTTP_TIMEOUT_S = 5

NOTEBOOK_SUFFIX = ".ipynb"
CELL_TYPES = ("code", "markdown", "raw")


async def list_sessions(jupyter_url: str, token: str) -> list[dict]:
    """Return the Jupyter server's open notebook sessions as raw /api/sessions records."""
    payload = await _get_json(jupyter_url, "api/sessions", token)
    return [s for s in payload if s.get("type") == "notebook"]


async def fetch_rooms(jupyter_url: str, token: str) -> dict:
    """Return the relay's live rooms as ``{notebook path: [roles]}``.

    An older extension without the rooms endpoint yields an empty mapping rather than an error, so
    discovery degrades to session data alone.
    """
    try:
        payload = await _get_json(jupyter_url, "mcp-bridge/rooms", token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return {}
        raise
    return payload.get("rooms", {})


def merge_notebook_view(sessions: list[dict], rooms: dict) -> list[dict]:
    """Combine session records and room presence into one record per open notebook.

    Notebooks appear if they have a session, a live room, or both; a browser tab that never started
    a kernel still shows up through its room.
    """
    view = {}
    for session in sessions:
        path = session.get("path")
        kernel = session.get("kernel") or {}
        view[path] = {
            "path": path,
            "kernel_state": kernel.get("execution_state", "unknown"),
            "browser_tab_connected": False,
            "assistant_attached": False,
        }
    for path, roles in rooms.items():
        record = view.setdefault(
            path,
            {"path": path, "kernel_state": "unknown", "browser_tab_connected": False, "assistant_attached": False},
        )
        record["browser_tab_connected"] = "extension" in roles
        record["assistant_attached"] = "mcp" in roles
    return sorted(view.values(), key=lambda record: record["path"])


def best_matches[T](requested: str, candidates: Sequence[T], key: Callable[[T], str] | None = None) -> list[T]:
    """Return the candidates whose path matches ``requested`` in the closest tier that matches at all.

    Tiers run exact path, case-insensitive path, case-insensitive basename, then substring. Return
    every candidate in that tier, so a tie is reported rather than resolved, and an empty list when
    nothing is close.

    Parameters
    ----------
    requested : str
        The path or fragment to match.
    candidates : list
        The values to match against.
    key : callable, optional
        Extracts the path from a candidate. Default treats each candidate as the path itself.
    """
    path_of = key if key is not None else (lambda candidate: candidate)
    exact = [candidate for candidate in candidates if path_of(candidate) == requested]
    if exact:
        return exact

    wanted = requested.strip().removeprefix("./").removeprefix("/")
    tiers = [
        lambda path: path == wanted,
        lambda path: path.lower() == wanted.lower(),
        lambda path: PurePosixPath(path).name.lower() == PurePosixPath(wanted).name.lower(),
        lambda path: wanted.lower() in path.lower(),
    ]
    for matches_tier in tiers:
        matched = [candidate for candidate in candidates if matches_tier(path_of(candidate))]
        if matched:
            return matched
    return []


def match_notebook(requested: str, available: list[str]) -> tuple[str | None, list[str]]:
    """Resolve ``requested`` against the open notebook paths.

    Return ``(match, candidates)``: a unique match, or ``None`` with the candidate paths that made
    the request ambiguous (empty when nothing was close).
    """
    matched = best_matches(requested, available)
    if len(matched) == 1:
        return matched[0], []
    return None, sorted(matched)


def notebook_url(jupyter_url: str, path: str) -> str:
    """Return the classic-UI URL a human opens to reach the notebook at ``path``."""
    return f"{jupyter_url.rstrip('/')}/notebooks/{quote(path.lstrip('/'))}"


async def create_notebook(
    jupyter_url: str, token: str, path: str, cells: list[dict] | None = None, kernel_name: str | None = None
) -> dict:
    """Create a notebook file at ``path`` and return the server's record of it.

    Raise RuntimeError when ``path`` does not end in ``.ipynb``, when a cell names an unknown type,
    or when something already occupies ``path`` -- the contents API would overwrite it without
    complaint.

    Parameters
    ----------
    jupyter_url : str
        Base URL of the Jupyter server to create the file on.
    token : str
        Authentication token for that server.
    path : str
        Server-relative path of the notebook, including the ``.ipynb`` suffix. Parent directories
        are not created.
    cells : list of dict, optional
        Seed cells as ``{cell_type, source}`` mappings, in order. Default None, an empty notebook.
    kernel_name : str, optional
        Kernel to record in the notebook's metadata. Default None, which leaves the notebook without
        a kernelspec so Jupyter picks its default when the notebook is opened.
    """
    if not path.endswith(NOTEBOOK_SUFFIX):
        # Jupyter types content by extension on every read, so a notebook saved under any other
        # name comes back as a plain file: invisible to list_notebooks, unopenable as a notebook.
        raise RuntimeError(f"a notebook path must end in {NOTEBOOK_SUFFIX}; got '{path}'")
    if await _path_exists(jupyter_url, token, path):
        raise RuntimeError(f"{path} already exists; pick another name, or open the one that is there")
    document = _notebook_document(cells or [], kernel_name)
    payload = {"type": "notebook", "format": "json", "content": document}
    return await _request_json("PUT", jupyter_url, f"api/contents/{quote(path)}", token, payload=payload)


def _notebook_document(cells: list[dict], kernel_name: str | None) -> dict:
    """Build an empty nbformat 4.5 notebook holding ``cells``."""
    metadata = {"kernelspec": {"name": kernel_name, "display_name": kernel_name}} if kernel_name else {}
    return {
        "cells": [_notebook_cell(cell) for cell in cells],
        "metadata": metadata,
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _notebook_cell(cell: dict) -> dict:
    """Expand a ``{cell_type, source}`` mapping into an nbformat cell with a fresh id.

    Raise RuntimeError on an unknown cell type: Jupyter captures the resulting validation failure
    into its reply instead of refusing the write, so an unchecked typo lands as a broken notebook.
    """
    cell_type = cell.get("cell_type", "code")
    if cell_type not in CELL_TYPES:
        raise RuntimeError(f"cell_type must be one of {', '.join(CELL_TYPES)}; got '{cell_type}'")
    built = {"cell_type": cell_type, "id": uuid4().hex[:8], "metadata": {}, "source": cell.get("source", "")}
    if cell_type == "code":
        built["execution_count"] = None
        built["outputs"] = []
    return built


async def _path_exists(jupyter_url: str, token: str, path: str) -> bool:
    """Report whether the Jupyter server already has something at ``path``."""
    try:
        await _get_json(jupyter_url, f"api/contents/{quote(path)}", token)
    except httpx.HTTPStatusError:
        # Only a 404 arrives here; _request_json turns every other status into a RuntimeError.
        return False
    return True


async def create_checkpoint(jupyter_url: str, token: str, path: str) -> dict:
    """Create a Jupyter checkpoint of ``path``'s saved file; return the checkpoint record.

    Overwrites the file's single default checkpoint slot -- the same one the notebook UI's
    "Save and Checkpoint" uses.
    """
    return await _post_json(jupyter_url, f"api/contents/{quote(path)}/checkpoints", token)


async def restore_checkpoint(jupyter_url: str, token: str, path: str, checkpoint_id: str) -> None:
    """Revert ``path``'s file on disk to ``checkpoint_id``. The browser buffer is untouched."""
    await _post_json(jupyter_url, f"api/contents/{quote(path)}/checkpoints/{quote(checkpoint_id)}", token)


async def list_checkpoints(jupyter_url: str, token: str, path: str) -> list[dict]:
    """Return the checkpoints recorded for ``path``."""
    return await _get_json(jupyter_url, f"api/contents/{quote(path)}/checkpoints", token)


async def _get_json(jupyter_url: str, endpoint: str, token: str):
    # The token travels in an Authorization header, never in the URL: httpx status errors embed
    # the full URL in their message, which surfaces in agent-facing tool errors and logs.
    return await _request_json("GET", jupyter_url, endpoint, token)


async def _post_json(jupyter_url: str, endpoint: str, token: str):
    return await _request_json("POST", jupyter_url, endpoint, token)


async def _request_json(method: str, jupyter_url: str, endpoint: str, token: str, payload: dict | None = None):
    url = f"{jupyter_url.rstrip('/')}/{endpoint}"
    headers = {"Authorization": f"token {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
            response = await client.request(method, url, headers=headers, json=payload)
            response.raise_for_status()
            return response.json() if response.content else None
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 404:
            raise  # fetch_rooms distinguishes a missing endpoint from a failure
        if status in (401, 403):
            raise RuntimeError(
                f"the Jupyter server at {jupyter_url} rejected this token ({status} for {endpoint}). "
                f"Call use_project with the project directory to derive the server and token from "
                f"that path, or use_server if the URL and token are already known."
            ) from exc
        raise RuntimeError(f"the Jupyter server at {jupyter_url} answered {status} for {endpoint}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"could not reach the Jupyter server at {jupyter_url}: {exc}") from exc
