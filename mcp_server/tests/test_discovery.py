import asyncio

import httpx
import pytest

from nbclassic_mcp_bridge_mcp.discovery import (
    create_checkpoint,
    fetch_rooms,
    list_sessions,
    match_notebook,
    merge_notebook_view,
    restore_checkpoint,
)


def _route_discovery_http(monkeypatch, handler):
    """Route discovery's HTTP through an httpx.MockTransport handler."""
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)
    )


OPEN_PATHS = ["notebooks/demo.ipynb", "notebooks/scratch.ipynb", "Untitled.ipynb"]


@pytest.mark.parametrize(
    "requested, expected",
    [
        ("notebooks/demo.ipynb", "notebooks/demo.ipynb"),
        ("./notebooks/demo.ipynb", "notebooks/demo.ipynb"),
        ("/notebooks/demo.ipynb", "notebooks/demo.ipynb"),
        ("NOTEBOOKS/DEMO.IPYNB", "notebooks/demo.ipynb"),
        ("demo.ipynb", "notebooks/demo.ipynb"),
        ("untitled.ipynb", "Untitled.ipynb"),
        ("scratch", "notebooks/scratch.ipynb"),
    ],
    ids=["exact", "dot-slash", "leading-slash", "case-insensitive", "basename", "basename-case", "substring"],
)
def test_match_notebook_resolves_uniquely(requested, expected):
    assert match_notebook(requested, OPEN_PATHS) == (expected, [])


def test_match_notebook_reports_ambiguity():
    match, candidates = match_notebook("notebooks", OPEN_PATHS)
    assert match is None
    assert candidates == ["notebooks/demo.ipynb", "notebooks/scratch.ipynb"]


def test_match_notebook_returns_nothing_for_a_miss():
    assert match_notebook("nonexistent.ipynb", OPEN_PATHS) == (None, [])


def test_match_notebook_prefers_a_case_insensitive_path_over_a_basename_match():
    # Tier order is contract: reordering silently changes which notebook a request resolves to.
    assert match_notebook("demo.ipynb", ["Demo.ipynb", "nb/demo.ipynb"]) == ("Demo.ipynb", [])


def test_match_notebook_prefers_exact_over_fuzzy():
    # "demo.ipynb" exists both as its own path and as the basename of another; exact wins.
    paths = ["demo.ipynb", "notebooks/demo.ipynb"]
    assert match_notebook("demo.ipynb", paths) == ("demo.ipynb", [])


def test_merge_notebook_view_combines_sessions_and_rooms():
    sessions = [
        {"type": "notebook", "path": "a.ipynb", "kernel": {"execution_state": "idle"}},
        {"type": "notebook", "path": "b.ipynb", "kernel": {"execution_state": "busy"}},
    ]
    rooms = {"b.ipynb": ["extension", "mcp"], "c.ipynb": ["extension"]}
    view = merge_notebook_view(sessions, rooms)
    assert [record["path"] for record in view] == ["a.ipynb", "b.ipynb", "c.ipynb"]
    by_path = {record["path"]: record for record in view}
    assert by_path["a.ipynb"] == {
        "path": "a.ipynb",
        "kernel_state": "idle",
        "browser_tab_connected": False,
        "assistant_attached": False,
    }
    assert by_path["b.ipynb"]["browser_tab_connected"] and by_path["b.ipynb"]["assistant_attached"]
    # A tab that never started a kernel still surfaces through its room.
    assert by_path["c.ipynb"]["kernel_state"] == "unknown"
    assert by_path["c.ipynb"]["browser_tab_connected"]


def test_merge_notebook_view_is_empty_when_nothing_is_open():
    assert merge_notebook_view([], {}) == []


def test_http_errors_never_leak_the_token(monkeypatch):
    seen = {}

    def handler(request):
        seen["auth_header"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        return httpx.Response(403)

    _route_discovery_http(monkeypatch, handler)
    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(list_sessions("http://localhost:1", "SECRET-TOKEN"))

    assert seen["auth_header"] == "token SECRET-TOKEN"
    assert "SECRET-TOKEN" not in seen["url"]
    assert "SECRET-TOKEN" not in str(exc_info.value)
    assert "403" in str(exc_info.value) and "JUPYTER_TOKEN" in str(exc_info.value)


def test_fetch_rooms_treats_a_missing_endpoint_as_no_rooms(monkeypatch):
    # An older extension without /mcp-bridge/rooms degrades discovery to session data alone.
    _route_discovery_http(monkeypatch, lambda request: httpx.Response(404))
    assert asyncio.run(fetch_rooms("http://localhost:1", "tok")) == {}


def test_unreachable_server_raises_a_clear_error(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused")

    _route_discovery_http(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="could not reach the Jupyter server"):
        asyncio.run(list_sessions("http://localhost:1", "tok"))


def test_checkpoint_helpers_hit_the_contents_api_with_header_auth(monkeypatch):
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path, request.headers.get("Authorization")))
        return httpx.Response(200, json={"id": "checkpoint"})

    _route_discovery_http(monkeypatch, handler)
    created = asyncio.run(create_checkpoint("http://localhost:1", "tok", "nb/analysis.ipynb"))
    asyncio.run(restore_checkpoint("http://localhost:1", "tok", "nb/analysis.ipynb", "checkpoint"))

    assert created == {"id": "checkpoint"}
    assert requests == [
        ("POST", "/api/contents/nb/analysis.ipynb/checkpoints", "token tok"),
        ("POST", "/api/contents/nb/analysis.ipynb/checkpoints/checkpoint", "token tok"),
    ]


def test_checkpoint_paths_are_url_quoted(monkeypatch):
    # A '#' in a notebook name would otherwise become a URL fragment and target a different file.
    seen = []

    def handler(request):
        seen.append(request.url.raw_path)
        return httpx.Response(200, json={})

    _route_discovery_http(monkeypatch, handler)
    asyncio.run(create_checkpoint("http://localhost:1", "tok", "my analysis#draft.ipynb"))
    assert seen == [b"/api/contents/my%20analysis%23draft.ipynb/checkpoints"]


def test_restore_tolerates_an_empty_reply_body(monkeypatch):
    # Jupyter answers checkpoint restores with 204 No Content.
    _route_discovery_http(monkeypatch, lambda request: httpx.Response(204))
    assert asyncio.run(restore_checkpoint("http://localhost:1", "tok", "a.ipynb", "checkpoint")) is None
