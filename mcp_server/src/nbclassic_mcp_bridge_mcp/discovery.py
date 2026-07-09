from pathlib import PurePosixPath

import httpx


_HTTP_TIMEOUT_S = 5


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


def match_notebook(requested: str, available: list[str]) -> tuple[str | None, list[str]]:
    """Resolve ``requested`` against the open notebook paths.

    Return ``(match, candidates)``: an exact or uniquely fuzzy match (case-insensitive path, then
    basename, then substring), or ``None`` with the candidate paths that made the request ambiguous
    (empty when nothing was close).
    """
    if requested in available:
        return requested, []

    wanted = requested.strip().removeprefix("./").removeprefix("/")
    tiers = [
        [p for p in available if p == wanted],
        [p for p in available if p.lower() == wanted.lower()],
        [p for p in available if PurePosixPath(p).name.lower() == PurePosixPath(wanted).name.lower()],
        [p for p in available if wanted.lower() in p.lower()],
    ]
    for matches in tiers:
        if len(matches) == 1:
            return matches[0], []
        if matches:
            return None, sorted(matches)
    return None, []


async def _get_json(jupyter_url: str, endpoint: str, token: str):
    # The token travels in an Authorization header, never in the URL: httpx status errors embed
    # the full URL in their message, which surfaces in agent-facing tool errors and logs.
    url = f"{jupyter_url.rstrip('/')}/{endpoint}"
    headers = {"Authorization": f"token {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise  # fetch_rooms distinguishes a missing endpoint from a failure
        raise RuntimeError(
            f"the Jupyter server at {jupyter_url} answered {exc.response.status_code} "
            f"for {endpoint} -- check JUPYTER_TOKEN"
        ) from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"could not reach the Jupyter server at {jupyter_url}: {exc}") from exc
