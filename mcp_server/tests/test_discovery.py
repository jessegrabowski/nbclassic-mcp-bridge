import asyncio
import json

import httpx
import pytest

from nbclassic_mcp_bridge_mcp.discovery import (
    best_matches,
    create_checkpoint,
    create_notebook,
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


def _creating_server(monkeypatch, existing=()):
    """Route creation traffic to a fake contents API; return the requests it received."""
    seen = []

    def handler(request):
        seen.append(request)
        path = request.url.path.removeprefix("/api/contents/")
        if request.method == "GET":
            if path in existing:
                return httpx.Response(200, json={"path": path, "type": "notebook"})
            return httpx.Response(404, json={"message": "no such file"})
        return httpx.Response(201, json={"path": path, "type": "notebook"})

    _route_discovery_http(monkeypatch, handler)
    return seen


def _written_document(requests):
    return json.loads(requests[-1].content)["content"]


def test_create_notebook_writes_a_valid_empty_notebook(monkeypatch):
    import nbformat

    requests = _creating_server(monkeypatch)
    asyncio.run(create_notebook("http://localhost:8888", "tok", "nb/new.ipynb"))

    assert [request.method for request in requests] == ["GET", "PUT"]
    document = _written_document(requests)
    nbformat.validate(nbformat.from_dict(document))
    assert (document["nbformat"], document["nbformat_minor"]) == (4, 5)
    assert document["cells"] == []


def test_create_notebook_expands_seed_cells(monkeypatch):
    import nbformat

    requests = _creating_server(monkeypatch)
    cells = [{"cell_type": "markdown", "source": "# Title"}, {"cell_type": "code", "source": "import pandas"}]
    asyncio.run(create_notebook("http://localhost:8888", "tok", "nb/new.ipynb", cells=cells))

    document = _written_document(requests)
    nbformat.validate(nbformat.from_dict(document))
    markdown, code = document["cells"]
    assert (markdown["cell_type"], markdown["source"]) == ("markdown", "# Title")
    assert "outputs" not in markdown  # a markdown cell carrying outputs fails nbformat validation
    assert (code["execution_count"], code["outputs"]) == (None, [])
    assert markdown["id"] != code["id"]


def test_create_notebook_records_the_kernel_when_asked(monkeypatch):
    import nbformat

    requests = _creating_server(monkeypatch)
    asyncio.run(create_notebook("http://localhost:8888", "tok", "nb/new.ipynb", kernel_name="python3"))

    document = _written_document(requests)
    nbformat.validate(nbformat.from_dict(document))
    assert document["metadata"]["kernelspec"]["name"] == "python3"


def test_create_notebook_leaves_the_kernel_to_jupyter_by_default(monkeypatch):
    requests = _creating_server(monkeypatch)
    asyncio.run(create_notebook("http://localhost:8888", "tok", "nb/new.ipynb"))
    assert "kernelspec" not in _written_document(requests)["metadata"]


def test_create_notebook_refuses_a_path_that_is_not_a_notebook(monkeypatch):
    # Jupyter types content by extension on read, so a notebook under any other name comes back a
    # plain file: invisible to list_notebooks and unopenable as a notebook.
    requests = _creating_server(monkeypatch)
    with pytest.raises(RuntimeError, match=r"must end in \.ipynb"):
        asyncio.run(create_notebook("http://localhost:8888", "tok", "nb/analysis"))
    assert requests == []


def test_create_notebook_refuses_an_unknown_cell_type(monkeypatch):
    # Jupyter captures the resulting validation failure into its reply rather than refusing the
    # write, so an unchecked typo would land as a broken notebook reported as created.
    requests = _creating_server(monkeypatch)
    with pytest.raises(RuntimeError, match="cell_type must be one of"):
        asyncio.run(
            create_notebook("http://localhost:8888", "tok", "nb/new.ipynb", cells=[{"cell_type": "Code", "source": ""}])
        )
    assert [request.method for request in requests] == ["GET"]


def test_create_notebook_refuses_an_existing_path(monkeypatch):
    requests = _creating_server(monkeypatch, existing={"nb/taken.ipynb"})
    with pytest.raises(RuntimeError, match="already exists"):
        asyncio.run(create_notebook("http://localhost:8888", "tok", "nb/taken.ipynb"))
    assert [request.method for request in requests] == ["GET"]


def test_create_notebook_keeps_the_token_out_of_its_errors(monkeypatch):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(404, json={"message": "no such file"})
        return httpx.Response(403, json={"message": "forbidden"})

    _route_discovery_http(monkeypatch, handler)
    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(create_notebook("http://localhost:8888", "SECRET-TOKEN", "nb/new.ipynb"))
    assert "SECRET-TOKEN" not in str(exc_info.value)


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


def test_best_matches_reads_the_path_through_a_key():
    # Attachments carry a server alongside the path, so the same path can appear twice and both
    # must come back rather than one being picked.
    candidates = [("a", "nb/report.ipynb"), ("b", "nb/report.ipynb"), ("a", "nb/other.ipynb")]
    matched = best_matches("report", candidates, key=lambda candidate: candidate[1])
    assert matched == [("a", "nb/report.ipynb"), ("b", "nb/report.ipynb")]


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
    message = str(exc_info.value)
    assert "403" in message
    # The recovery has to be reachable from inside the session, so the message names the tools that
    # re-point the bridge rather than an environment variable the caller cannot set.
    assert "use_project" in message and "use_server" in message


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
