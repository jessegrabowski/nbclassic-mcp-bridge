import asyncio
import hashlib
import inspect

import pytest

from nbclassic_mcp_bridge_mcp.registry import Attachment, NotebookRegistry
from nbclassic_mcp_bridge_mcp.server import (
    _JUPYTER_URL,
    _OUTPUT_CHAR_LIMIT,
    _cell_output_view,
    _cell_source_view,
    _clean_output,
    _derive_endpoint,
    _extract_images,
    _outline_cell,
    _summarize_output,
    _truncate,
)


def test_derive_endpoint_hashes_physical_path(tmp_path):
    url, token = _derive_endpoint(str(tmp_path))
    assert url == _JUPYTER_URL
    assert token == hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()


def test_derive_endpoint_resolves_symlinks(tmp_path):
    real = tmp_path / "project"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert _derive_endpoint(str(link)) == _derive_endpoint(str(real))


def test_truncate_respects_the_limit_and_leaves_short_text():
    assert _truncate("short") == "short"
    assert _truncate("abcdef", limit=3).startswith("abc")
    assert "truncated" in _truncate("x" * 10000)


def test_clean_output_strips_image_and_truncates_text():
    output = {
        "output_type": "display_data",
        "data": {"image/png": "BASE64BLOB", "text/plain": "y" * 10000},
    }
    _clean_output(output)
    assert output["data"]["image/png"].startswith("<image/png omitted")
    assert "truncated" in output["data"]["text/plain"]


def test_clean_output_truncates_stream_text_and_traceback():
    stream = {"output_type": "stream", "name": "stdout", "text": "z" * 10000}
    _clean_output(stream)
    assert "truncated" in stream["text"]

    err = {"output_type": "error", "ename": "ValueError", "traceback": ["line\n"] * 5000}
    _clean_output(err)
    assert len(err["traceback"]) == 1
    assert "truncated" in err["traceback"][0]


def test_clean_output_without_truncate_keeps_text_but_still_strips_images():
    output = {
        "output_type": "display_data",
        "data": {"image/png": "BASE64BLOB", "text/plain": "y" * 10000},
    }
    _clean_output(output, truncate=False)
    assert output["data"]["image/png"].startswith("<image/png omitted")
    assert output["data"]["text/plain"] == "y" * 10000


def test_stripped_image_names_the_call_that_fetches_it():
    output = {"output_type": "display_data", "data": {"image/png": "BASE64BLOB"}}
    _clean_output(output, cell_id="abc123")
    placeholder = output["data"]["image/png"]
    assert 'read_cell_image(cell_id="abc123")' in placeholder

    # Kernel introspection produces no cell, so there is no id to fetch the image by.
    loose = {"output_type": "display_data", "data": {"image/png": "BASE64BLOB"}}
    _clean_output(loose)
    assert "read_cell_image" in loose["data"]["image/png"]
    assert "cell_id=" not in loose["data"]["image/png"]


def test_summarize_output_describes_without_payload():
    stream = _summarize_output({"output_type": "stream", "name": "stdout", "text": "abcde"})
    assert stream == {"output_type": "stream", "name": "stdout", "chars": 5}

    rich = _summarize_output({"output_type": "execute_result", "data": {"text/plain": "1", "image/png": "blob"}})
    assert rich == {"output_type": "execute_result", "mime_types": ["image/png", "text/plain"]}


def test_outline_cell_summarizes_outputs_and_truncates_runaway_source():
    cell = {
        "cell_id": "c1",
        "index": 2,
        "cell_type": "code",
        "source": "print(1)",
        "outputs": [{"output_type": "stream", "name": "stdout", "text": "1\n"}],
    }
    outlined = _outline_cell(cell)
    assert outlined["source"] == "print(1)"
    assert "outputs" not in outlined
    assert outlined["output_summary"] == [{"output_type": "stream", "name": "stdout", "chars": 2}]

    runaway = _outline_cell({"cell_id": "c2", "cell_type": "code", "source": "x" * 100000})
    assert "truncated" in runaway["source"]


def test_outline_cell_passes_through_an_extension_side_summary():
    # A summary snapshot carries output_summary instead of outputs; keep it as is.
    summary = [{"output_type": "execute_result", "mime_types": ["image/png"]}]
    cell = {"cell_id": "c1", "index": 0, "cell_type": "code", "source": "plot()", "output_summary": summary}
    assert _outline_cell(cell)["output_summary"] == summary


def test_extract_images_collects_raster_payloads_in_document_order():
    cell = {
        "outputs": [
            {"output_type": "stream", "name": "stdout", "text": "no data dict"},
            {"output_type": "display_data", "data": {"image/png": ["QUJD", "REVG"], "text/plain": "fig1"}},
            {"output_type": "execute_result", "data": {"image/svg+xml": "<svg/>", "image/jpeg": "R0lG"}},
        ]
    }
    images = _extract_images(cell)
    # multiline base64 is joined; SVG (text, not base64) is skipped
    assert images == [("image/png", "QUJDREVG"), ("image/jpeg", "R0lG")]


def test_extract_images_returns_empty_for_imageless_cells():
    assert _extract_images({"outputs": [{"output_type": "stream", "text": "hi"}]}) == []
    assert _extract_images({}) == []


def test_cell_source_view_returns_source_without_outputs():
    cell = {
        "cell_id": "c1",
        "index": 3,
        "cell_type": "code",
        "source": "print(1)",
        "outputs": [{"output_type": "stream", "text": "1"}],
    }
    view = _cell_source_view(cell, full=False)
    assert view == {"cell_id": "c1", "index": 3, "cell_type": "code", "source": "print(1)"}
    assert "outputs" not in view


def test_cell_source_view_full_skips_truncation():
    cell = {"cell_id": "c1", "source": "x" * 100000}
    assert "truncated" in _cell_source_view(cell, full=False)["source"]
    assert _cell_source_view(cell, full=True)["source"] == "x" * 100000


def test_cell_output_view_returns_outputs_without_source():
    cell = {
        "cell_id": "c1",
        "cell_type": "code",
        "source": "print(1)",
        "outputs": [{"output_type": "stream", "name": "stdout", "text": "1\n"}],
    }
    view = _cell_output_view(cell, full=False)
    assert view == {
        "cell_id": "c1",
        "outputs": [{"output_type": "stream", "name": "stdout", "text": "1\n"}],
    }
    assert "source" not in view


def test_cell_output_view_full_skips_text_truncation():
    def fresh_cell():
        return {"cell_id": "c1", "outputs": [{"output_type": "stream", "text": "z" * 10000}]}

    assert "truncated" in _cell_output_view(fresh_cell(), full=False)["outputs"][0]["text"]
    assert _cell_output_view(fresh_cell(), full=True)["outputs"][0]["text"] == "z" * 10000


def _record(path, tab=True):
    return {"path": path, "kernel_state": "idle", "browser_tab_connected": tab, "assistant_attached": False}


DEFAULT_URL = "http://a:8888"
TOKEN = "tok-a"
OTHER_URL = "http://b:9999"
OTHER_TOKEN = "tok-b"
NEW_NOTEBOOK_URL = f"{DEFAULT_URL}/notebooks/nb/new.ipynb"


def _run_use_notebook(monkeypatch, notebooks, path=None, tab_connected=True):
    """Drive use_notebook against a real registry; return its message and the registry."""
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: notebooks}, tab_connected=tab_connected)
    return asyncio.run(server.use_notebook(path)), registry


def _attached_paths(registry):
    return [attachment.path for attachment in registry.attachments()]


def test_use_notebook_auto_attaches_the_only_open_notebook(monkeypatch):
    message, registry = _run_use_notebook(monkeypatch, [_record("nb/a.ipynb")])
    assert _attached_paths(registry) == ["nb/a.ipynb"]
    assert "attached to nb/a.ipynb" in message and "browser tab connected" in message


def test_use_notebook_prefers_the_notebook_with_a_live_tab(monkeypatch):
    notebooks = [_record("nb/a.ipynb", tab=False), _record("nb/b.ipynb", tab=True)]
    _, registry = _run_use_notebook(monkeypatch, notebooks)
    assert _attached_paths(registry) == ["nb/b.ipynb"]


def test_use_notebook_lists_options_instead_of_guessing(monkeypatch):
    notebooks = [_record("nb/a.ipynb"), _record("nb/b.ipynb")]
    message, registry = _run_use_notebook(monkeypatch, notebooks)
    assert _attached_paths(registry) == []
    assert "nb/a.ipynb" in message and "nb/b.ipynb" in message


def test_use_notebook_resolves_a_fuzzy_path(monkeypatch):
    message, registry = _run_use_notebook(monkeypatch, [_record("notebooks/demo.ipynb")], path="demo.ipynb")
    assert _attached_paths(registry) == ["notebooks/demo.ipynb"]
    assert "resolved from 'demo.ipynb'" in message


def test_use_notebook_reports_ambiguous_paths_without_attaching(monkeypatch):
    notebooks = [_record("a/report.ipynb"), _record("b/report.ipynb")]
    message, registry = _run_use_notebook(monkeypatch, notebooks, path="report.ipynb")
    assert _attached_paths(registry) == []
    assert "a/report.ipynb" in message and "b/report.ipynb" in message


def test_use_notebook_attaches_verbatim_when_nothing_matches(monkeypatch):
    # The room outlives the call, so attaching before the human opens the notebook still works.
    message, registry = _run_use_notebook(monkeypatch, [_record("nb/a.ipynb")], path="new.ipynb", tab_connected=False)
    assert _attached_paths(registry) == ["new.ipynb"]
    assert "no browser tab is connected" in message and "nb/a.ipynb" in message


@pytest.mark.parametrize("tab_connected", [True, False], ids=["tab-connected", "no-tab"])
def test_use_notebook_names_the_other_attachments(monkeypatch, tab_connected):
    # The assistant is re-told what else it holds rather than having to remember across calls, on
    # both replies -- they are analogous by design, which is where one quietly drifts.
    server, _ = _with_registry(
        monkeypatch,
        {DEFAULT_URL: [_record("nb/a.ipynb"), _record("nb/b.ipynb")]},
        tab_connected=tab_connected,
    )

    async def scenario():
        await server.use_notebook("nb/a.ipynb")
        return await server.use_notebook("nb/b.ipynb")

    message = asyncio.run(scenario())
    assert "attached to nb/b.ipynb" in message
    assert "also attached: nb/a.ipynb" in message


class FakeRelayClient:
    """Minimal relay client for driving a real NotebookRegistry through the tools."""

    def __init__(self, jupyter_url, token, on_event=None):
        self.jupyter_url = jupyter_url
        self.token = token
        self.on_event = on_event
        self.notebook = None
        self.commands: list[tuple[str, dict]] = []
        # Replies keyed by op; anything unlisted answers with an empty result. None stands for a
        # reply that carried no result payload at all.
        self.results: dict[str, dict | None] = {}
        self.fails_with: str | None = None

    async def connect(self, path):
        self.notebook = path

    async def close(self):
        pass

    async def command(self, op, args):
        self.commands.append((op, args))
        if self.fails_with is not None:
            raise RuntimeError(self.fails_with)
        return self.results.get(op, {})

    async def extension_present(self, timeout=0.5):
        return self.tab_connected

    def emit(self, name, data=None):
        """Deliver one event the way the read loop does, once it has cleared replay dedup."""
        self.on_event({"kind": "event", "name": name, "data": data or {}})


def _with_registry(monkeypatch, open_notebooks, unreachable=(), tab_connected=True):
    """Install a real registry backed by relay stubs.

    ``open_notebooks`` maps a Jupyter URL to the discovery records that server reports; a URL in
    ``unreachable`` raises instead, standing in for a server that is not running.
    """
    import nbclassic_mcp_bridge_mcp.server as server

    def factory(jupyter_url, token, on_event=None):
        client = FakeRelayClient(jupyter_url, token, on_event)
        client.tab_connected = tab_connected
        return client

    registry = NotebookRegistry(DEFAULT_URL, TOKEN, client_factory=factory)
    monkeypatch.setattr(server, "_registry", registry)

    async def fake_open(jupyter_url, token):
        if jupyter_url in unreachable:
            raise RuntimeError(f"could not reach the Jupyter server at {jupyter_url}")
        return [dict(record) for record in open_notebooks.get(jupyter_url, [])]

    monkeypatch.setattr(server, "_open_notebooks", fake_open)
    return server, registry


def test_create_notebook_writes_to_the_default_server(monkeypatch):
    # Creation targets where the next use_notebook would look, not whichever notebook is attached.
    server, _ = _with_offset_default(monkeypatch)
    created = []

    async def create(jupyter_url, token, path, cells, kernel_name):
        created.append((jupyter_url, token, path, cells, kernel_name))
        return {"path": path}

    monkeypatch.setattr(server.discovery, "create_notebook", create)
    message = asyncio.run(server.create_notebook("nb/new.ipynb", cells=[{"cell_type": "code", "source": "1"}]))

    assert created == [(OTHER_URL, OTHER_TOKEN, "nb/new.ipynb", [{"cell_type": "code", "source": "1"}], None)]
    assert "created nb/new.ipynb" in message and "use_notebook" in message


def test_create_notebook_reports_the_path_the_server_used(monkeypatch):
    # Jupyter normalizes what it was given -- a leading slash is stripped -- so echoing the request
    # back would name a path that does not exist as written.
    server, _ = _with_registry(monkeypatch, {})

    async def create(jupyter_url, token, path, cells, kernel_name):
        return {"path": path.lstrip("/")}

    monkeypatch.setattr(server.discovery, "create_notebook", create)
    message = asyncio.run(server.create_notebook("/nb/new.ipynb"))
    assert "created nb/new.ipynb" in message


def _rooms_returning(monkeypatch, server, *answers):
    """Answer successive fetch_rooms calls from ``answers``, repeating the last one forever."""
    calls = []

    async def fake_rooms(jupyter_url, token):
        calls.append(jupyter_url)
        return answers[min(len(calls) - 1, len(answers) - 1)]

    monkeypatch.setattr(server.discovery, "fetch_rooms", fake_rooms)
    monkeypatch.setattr(server, "_ROOM_POLL_INTERVAL_S", 0)
    return calls


def test_attach_when_tab_arrives_waits_for_the_room_to_gain_an_extension(monkeypatch):
    server, registry = _with_registry(monkeypatch, {})
    calls = _rooms_returning(monkeypatch, server, {}, {"nb/new.ipynb": ["mcp"]}, {"nb/new.ipynb": ["extension"]})

    client = asyncio.run(server._attach_when_tab_arrives("nb/new.ipynb", timeout=5))

    assert client is not None
    # A room with only an mcp peer is a room nobody's browser is in; polling has to continue.
    assert len(calls) == 3
    assert [attachment.path for attachment in registry.attachments()] == ["nb/new.ipynb"]


def test_attach_when_tab_arrives_gives_up_without_attaching(monkeypatch):
    server, registry = _with_registry(monkeypatch, {})
    _rooms_returning(monkeypatch, server, {})

    assert asyncio.run(server._attach_when_tab_arrives("nb/new.ipynb", timeout=0)) is None
    assert registry.attachments() == []


def test_the_tab_asked_to_open_is_the_current_one(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb"), _record("nb/b.ipynb")]})

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")
        # Current is deliberately not the newest attachment, or picking either would look right.
        registry.make_current(Attachment(DEFAULT_URL, "nb/a.ipynb"))
        return await server._tab_that_can_open()

    assert asyncio.run(scenario()) is registry.client_for(Attachment(DEFAULT_URL, "nb/a.ipynb"))


def test_a_disconnected_current_tab_falls_through_to_another(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb"), _record("nb/b.ipynb")]})

    async def scenario():
        await registry.attach("nb/a.ipynb")
        current = await registry.attach("nb/b.ipynb")
        current.tab_connected = False
        return await server._tab_that_can_open()

    assert asyncio.run(scenario()) is registry.client_for(Attachment(DEFAULT_URL, "nb/a.ipynb"))


def test_a_tab_on_another_server_is_never_asked_to_open(monkeypatch):
    # A tab can only open notebooks its own server serves, so one on another server is no help.
    server, _ = _with_offset_default(monkeypatch)
    assert asyncio.run(server._tab_that_can_open()) is None


def test_a_disconnected_tab_is_not_asked_to_open(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb")]}, tab_connected=False)

    async def scenario():
        await registry.attach("nb/a.ipynb")
        return await server._tab_that_can_open()

    assert asyncio.run(scenario()) is None


def test_create_notebook_surfaces_a_refusal(monkeypatch):
    server, _ = _with_registry(monkeypatch, {})

    async def create(jupyter_url, token, path, cells, kernel_name):
        raise RuntimeError(f"{path} already exists; pick another name, or open the one that is there")

    monkeypatch.setattr(server.discovery, "create_notebook", create)
    with pytest.raises(RuntimeError, match="already exists"):
        asyncio.run(server.create_notebook("nb/taken.ipynb"))


def _creating(monkeypatch, server):
    """Make discovery.create_notebook succeed, echoing back the path it was given."""

    async def create(jupyter_url, token, path, cells, kernel_name):
        return {"path": path}

    monkeypatch.setattr(server.discovery, "create_notebook", create)


def test_create_notebook_open_asks_a_tab_then_attaches(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb")]})
    _creating(monkeypatch, server)
    _rooms_returning(monkeypatch, server, {"nb/new.ipynb": ["extension"]})

    async def scenario():
        opener = await registry.attach("nb/a.ipynb")
        opener.results["open_notebook"] = {"opened": True, "url": NEW_NOTEBOOK_URL}
        return await server.create_notebook("nb/new.ipynb", open=True), opener

    message, opener = asyncio.run(scenario())
    assert opener.commands == [("open_notebook", {"path": "nb/new.ipynb"})]
    assert "opened it in a new tab, and attached" in message
    assert registry.is_current(Attachment(DEFAULT_URL, "nb/new.ipynb"))


def test_create_notebook_open_says_what_to_open_when_no_tab_can_be_asked(monkeypatch):
    server, registry = _with_registry(monkeypatch, {})
    _creating(monkeypatch, server)

    message = asyncio.run(server.create_notebook("nb/new.ipynb", open=True))

    assert "no attached tab could be asked" in message
    assert NEW_NOTEBOOK_URL in message
    assert registry.attachments() == []


@pytest.mark.parametrize(
    ("reply", "expected_reason"),
    # None stands for a reply that carried no result payload: command() returns reply["result"], so
    # a peer answering ok without one hands back None, and a raw AttributeError there would bury
    # the fact that the file was created.
    [({"opened": False, "reason": "popup blocked"}, "popup blocked"), (None, "no reason given")],
    ids=["refused with a reason", "reply carrying no result"],
)
def test_create_notebook_open_reports_a_refusal_with_the_url(monkeypatch, reply, expected_reason):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb")]})
    _creating(monkeypatch, server)

    async def scenario():
        opener = await registry.attach("nb/a.ipynb")
        opener.results["open_notebook"] = reply
        return await server.create_notebook("nb/new.ipynb", open=True)

    message = asyncio.run(scenario())
    assert "created nb/new.ipynb" in message
    assert expected_reason in message
    assert NEW_NOTEBOOK_URL in message
    assert not registry.is_attached(Attachment(DEFAULT_URL, "nb/new.ipynb"))


def test_create_notebook_open_reports_a_tab_that_never_reaches_the_bridge(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb")]})
    _creating(monkeypatch, server)
    _rooms_returning(monkeypatch, server, {})
    monkeypatch.setattr(server, "_TAB_ARRIVAL_TIMEOUT_S", 0)

    async def scenario():
        opener = await registry.attach("nb/a.ipynb")
        opener.results["open_notebook"] = {"opened": True}
        return await server.create_notebook("nb/new.ipynb", open=True)

    message = asyncio.run(scenario())
    assert "has not reached the bridge" in message
    assert not registry.is_attached(Attachment(DEFAULT_URL, "nb/new.ipynb"))


def test_create_notebook_open_keeps_the_file_when_the_tab_rejects_the_op(monkeypatch):
    # An older extension without open_notebook must not turn a created file into a bare exception.
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb")]})
    _creating(monkeypatch, server)

    async def scenario():
        opener = await registry.attach("nb/a.ipynb")
        opener.fails_with = "the notebook tab's bridge extension does not support 'open_notebook'"
        return await server.create_notebook("nb/new.ipynb", open=True)

    message = asyncio.run(scenario())
    assert "created nb/new.ipynb" in message
    assert "does not support 'open_notebook'" in message
    assert NEW_NOTEBOOK_URL in message


def _clients_by_path(registry):
    return {attachment.path: registry.client_for(attachment) for attachment in registry.attachments()}


def test_list_notebooks_survives_an_unreachable_server(monkeypatch):
    # list_notebooks is what the assistant reaches for once it has lost track of state, and the
    # default endpoint is routinely a server that is not running -- so one dead server must not
    # hide the notebooks on the live ones.
    server, registry = _with_registry(monkeypatch, {OTHER_URL: [_record("nb/a.ipynb")]}, unreachable={DEFAULT_URL})

    async def scenario():
        registry.retarget(OTHER_URL, OTHER_TOKEN)
        await registry.attach("nb/a.ipynb")
        registry.retarget(DEFAULT_URL, TOKEN)
        return await server.list_notebooks()

    records = asyncio.run(scenario())
    failed = [record for record in records if "error" in record]
    live = [record for record in records if "error" not in record]
    assert [record["jupyter_url"] for record in failed] == [DEFAULT_URL]
    assert "could not reach" in failed[0]["error"]
    assert [record["path"] for record in live] == ["nb/a.ipynb"]
    assert live[0]["attached"] is True


def test_list_notebooks_marks_attached_and_current(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb"), _record("nb/b.ipynb")]})
    asyncio.run(registry.attach("nb/a.ipynb"))

    by_path = {record["path"]: record for record in asyncio.run(server.list_notebooks())}
    assert (by_path["nb/a.ipynb"]["attached"], by_path["nb/a.ipynb"]["current"]) == (True, True)
    assert (by_path["nb/b.ipynb"]["attached"], by_path["nb/b.ipynb"]["current"]) == (False, False)
    # A single-server session never sees a URL.
    assert "jupyter_url" not in by_path["nb/a.ipynb"]


def test_list_notebooks_spans_servers_once_two_are_in_play(monkeypatch):
    server, registry = _with_registry(
        monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb")], OTHER_URL: [_record("nb/a.ipynb")]}
    )

    async def scenario():
        await registry.attach("nb/a.ipynb")
        registry.retarget(OTHER_URL, OTHER_TOKEN)
        await registry.attach("nb/a.ipynb")
        return await server.list_notebooks()

    records = asyncio.run(scenario())
    assert sorted(record["jupyter_url"] for record in records) == [DEFAULT_URL, OTHER_URL]
    assert all(record["attached"] for record in records)
    assert [record["current"] for record in records].count(True) == 1


def test_detach_notebook_leaves_the_other_attachments(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb"), _record("nb/b.ipynb")]})

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")
        return await server.detach_notebook("nb/a.ipynb")

    message = asyncio.run(scenario())
    assert "detached nb/a.ipynb" in message and "still attached: nb/b.ipynb" in message
    assert registry.is_current(Attachment(DEFAULT_URL, "nb/b.ipynb"))


def test_detaching_the_current_notebook_says_there_is_none(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb"), _record("nb/b.ipynb")]})

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")
        return await server.detach_notebook("nb/b.ipynb")

    message = asyncio.run(scenario())
    assert "no current notebook" in message
    with pytest.raises(RuntimeError, match="no current notebook"):
        registry.current()


def test_detach_notebook_with_nothing_attached_says_none(monkeypatch):
    # Detaching before ever attaching is an ordinary mistake, and it renders the empty listing.
    server, _ = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb")]})
    with pytest.raises(RuntimeError, match="not attached; attached: none"):
        asyncio.run(server.detach_notebook("nb/a.ipynb"))


def test_detach_notebook_rejects_an_unattached_path(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb")]})
    asyncio.run(registry.attach("nb/a.ipynb"))
    with pytest.raises(RuntimeError, match=r"not attached; attached: nb/a\.ipynb"):
        asyncio.run(server.detach_notebook("nb/zzz.ipynb"))


def test_detach_notebook_asks_for_a_narrower_name_when_several_match(monkeypatch):
    # Ambiguity is guidance, not failure -- the same answer use_notebook gives when several
    # notebooks match, so the assistant's next move is a better call rather than a recovery.
    server, registry = _with_registry(
        monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb")], OTHER_URL: [_record("nb/a.ipynb")]}
    )

    async def scenario():
        await registry.attach("nb/a.ipynb")
        registry.retarget(OTHER_URL, OTHER_TOKEN)
        await registry.attach("nb/a.ipynb")
        return await server.detach_notebook("nb/a.ipynb")

    message = asyncio.run(scenario())
    assert "matches several attached notebooks" in message
    assert DEFAULT_URL in message and OTHER_URL in message
    assert len(registry.attachments()) == 2


def test_detach_notebook_ambiguity_does_not_blame_servers_when_one_holds_both(monkeypatch):
    # A loose name can tie two notebooks on a single server; the reply must not send the assistant
    # to use_server, which cannot separate them.
    server, registry = _with_registry(
        monkeypatch, {DEFAULT_URL: [_record("chapter1/report.ipynb"), _record("chapter2/report.ipynb")]}
    )

    async def scenario():
        await registry.attach("chapter1/report.ipynb")
        await registry.attach("chapter2/report.ipynb")
        return await server.detach_notebook("report")

    message = asyncio.run(scenario())
    assert "matches several attached notebooks" in message
    assert "use_server" not in message
    assert len(registry.attachments()) == 2


# Tools that pick, describe, or create a notebook rather than acting on an attached one; everything
# else must accept a notebook to act on. Derived by subtraction so a new tool opts out deliberately.
ATTACHMENT_TOOLS = {
    "list_notebooks",
    "use_notebook",
    "use_server",
    "use_project",
    "detach_notebook",
    "poll_events",
    "create_notebook",
}


def test_every_notebook_scoped_tool_accepts_a_target():
    import nbclassic_mcp_bridge_mcp.server as server

    registered = asyncio.run(server.mcp.list_tools())
    missing = sorted(
        tool.name
        for tool in registered
        if tool.name not in ATTACHMENT_TOOLS
        and "notebook" not in inspect.signature(getattr(server, tool.name)).parameters
    )
    assert missing == []


# Tools whose result must name the notebook it acted on, and a call that exercises each.
ECHOING_TOOLS = [
    ("insert_cell", {"index": 0, "cell_type": "code", "source": "x"}),
    ("set_cell_source", {"cell_id": "c1", "source": "x"}),
    ("delete_cell", {"cell_id": "c1"}),
    ("move_cell", {"cell_id": "c1", "index": 1}),
    ("execute_cell", {"cell_id": "c1"}),
    ("run_cells", {"cell_ids": ["c1"]}),
    ("undo_last_change", {}),
    ("undo_all_changes", {}),
]


@pytest.mark.parametrize("tool_name, kwargs", ECHOING_TOOLS, ids=[name for name, _ in ECHOING_TOOLS])
def test_a_mutating_tool_acts_on_the_named_notebook(monkeypatch, tool_name, kwargs):
    # Two halves of one contract: the result says which notebook was changed, so the assistant is
    # told rather than having to remember, and the named notebook becomes the current one.
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb"), _record("nb/b.ipynb")]})

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")
        return await getattr(server, tool_name)(**kwargs, notebook="nb/a.ipynb")

    assert asyncio.run(scenario())["notebook"] == "nb/a.ipynb"
    assert registry.is_current(Attachment(DEFAULT_URL, "nb/a.ipynb"))


READING_TOOLS = [
    ("read_notebook", {}),
    ("read_cell_source", {"cell_id": "c1"}),
    ("read_cell_output", {"cell_id": "c1"}),
    ("inspect_kernel", {"code": "x"}),
    ("kernel_status", {}),
]


@pytest.mark.parametrize("tool_name, kwargs", READING_TOOLS, ids=[name for name, _ in READING_TOOLS])
def test_reading_another_notebook_does_not_retarget(monkeypatch, tool_name, kwargs):
    # Peeking at a second notebook must not silently redirect the next mutation -- only changing or
    # executing one says the assistant is working there.
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb"), _record("nb/b.ipynb")]})

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")
        await getattr(server, tool_name)(**kwargs, notebook="nb/a.ipynb")

    asyncio.run(scenario())
    assert registry.is_current(Attachment(DEFAULT_URL, "nb/b.ipynb"))


# Notebook-scoped tools whose targeting no other test proves. A tool can accept ``notebook`` and
# quietly ignore it, which the signature check above cannot see, so assert the command actually
# reaches the named notebook's client. read_cell_image dispatches and then rejects the imageless
# stub reply; the dispatch is what is under test.
DISPATCHING_TOOLS = [
    ("read_cell_image", {"cell_id": "c1"}, RuntimeError),
    ("interrupt_kernel", {}, None),
    ("restart_kernel", {}, None),
]


@pytest.mark.parametrize("timeout_s, expected_ms", [(None, 60000), (5, 5000), (0.25, 250)])
def test_restart_kernel_sends_its_timeout_in_milliseconds(monkeypatch, timeout_s, expected_ms):
    # The extension reads timeout_ms, so dropping the conversion would ask for a 60 millisecond
    # restart and report every restart as timed out. No e2e test covers this: those drive the wire
    # op directly and never run the tool function.
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb")]})

    async def scenario():
        await registry.attach("nb/a.ipynb")
        kwargs = {} if timeout_s is None else {"timeout_s": timeout_s}
        await server.restart_kernel(**kwargs)

    asyncio.run(scenario())

    op, args = _clients_by_path(registry)["nb/a.ipynb"].commands[-1]
    assert op == "restart_kernel"
    assert args == {"timeout_ms": expected_ms}


@pytest.mark.parametrize(
    "tool_name, kwargs, expected_error", DISPATCHING_TOOLS, ids=[name for name, _, _ in DISPATCHING_TOOLS]
)
def test_a_tool_dispatches_to_the_named_notebook(monkeypatch, tool_name, kwargs, expected_error):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb"), _record("nb/b.ipynb")]})

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")
        await getattr(server, tool_name)(**kwargs, notebook="nb/a.ipynb")

    if expected_error is None:
        asyncio.run(scenario())
    else:
        with pytest.raises(expected_error):
            asyncio.run(scenario())

    by_path = _clients_by_path(registry)
    assert by_path["nb/a.ipynb"].commands != []
    assert by_path["nb/b.ipynb"].commands == []


def test_checkpoint_targets_the_named_notebook(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb"), _record("nb/b.ipynb")]})
    checkpointed = []

    async def create(url, token, path):
        checkpointed.append(path)
        return {"id": "cp1"}

    monkeypatch.setattr(server.discovery, "create_checkpoint", create)

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")
        return await server.checkpoint_notebook(notebook="nb/a.ipynb")

    assert asyncio.run(scenario())["notebook"] == "nb/a.ipynb"
    assert checkpointed == ["nb/a.ipynb"]


def test_restore_targets_the_named_notebook(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb"), _record("nb/b.ipynb")]})
    restored = []

    async def list_checkpoints(url, token, path):
        return [{"id": "cp1"}]

    async def restore(url, token, path, checkpoint_id):
        restored.append(path)

    monkeypatch.setattr(server.discovery, "list_checkpoints", list_checkpoints)
    monkeypatch.setattr(server.discovery, "restore_checkpoint", restore)

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")
        return await server.restore_notebook_checkpoint(notebook="nb/a.ipynb")

    message = asyncio.run(scenario())
    assert restored == ["nb/a.ipynb"]
    assert "restored nb/a.ipynb" in message
    by_path = _clients_by_path(registry)
    assert by_path["nb/a.ipynb"].commands == [("reload_notebook", {})]
    assert by_path["nb/b.ipynb"].commands == []


def test_kernel_status_names_the_notebook_it_describes(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb")]})

    async def scenario():
        await registry.attach("nb/a.ipynb")
        return await server.kernel_status()

    assert asyncio.run(scenario())["notebook"] == "nb/a.ipynb"


def test_a_tool_targets_a_notebook_by_a_loose_name(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("notebooks/2026/analysis.ipynb")]})

    async def scenario():
        await registry.attach("notebooks/2026/analysis.ipynb")
        return await server.delete_cell("c1", notebook="analysis")

    assert asyncio.run(scenario())["notebook"] == "notebooks/2026/analysis.ipynb"


def test_a_tool_refuses_an_unattached_notebook(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb")]})
    asyncio.run(registry.attach("nb/a.ipynb"))
    with pytest.raises(RuntimeError, match="is not attached"):
        asyncio.run(server.delete_cell("c1", notebook="nb/zzz.ipynb"))


def test_a_tool_refuses_an_ambiguous_notebook_rather_than_guessing(monkeypatch):
    # A mutation must never land on a coin flip, so ambiguity raises here even though the picker
    # tools answer the same condition with guidance.
    server, registry = _with_registry(
        monkeypatch, {DEFAULT_URL: [_record("a/report.ipynb"), _record("b/report.ipynb")]}
    )

    async def scenario():
        await registry.attach("a/report.ipynb")
        await registry.attach("b/report.ipynb")
        await server.delete_cell("c1", notebook="report.ipynb")

    with pytest.raises(RuntimeError, match="matches several attached notebooks"):
        asyncio.run(scenario())


def test_omitting_the_notebook_targets_the_current_one(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb"), _record("nb/b.ipynb")]})

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")
        return await server.delete_cell("c1")

    assert asyncio.run(scenario())["notebook"] == "nb/b.ipynb"


def test_poll_events_names_the_notebook_each_event_came_from(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb"), _record("nb/b.ipynb")]})

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")
        clients = _clients_by_path(registry)
        clients["nb/a.ipynb"].emit("cell_created")
        clients["nb/b.ipynb"].emit("source_changed")
        return await server.poll_events()

    result = asyncio.run(scenario())
    assert [(e["notebook"], e["name"]) for e in result["events"]] == [
        ("nb/a.ipynb", "cell_created"),
        ("nb/b.ipynb", "source_changed"),
    ]
    assert result["cursor"] == 2


def test_poll_events_filters_by_notebook_without_rewinding_the_cursor(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb"), _record("nb/b.ipynb")]})

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")
        clients = _clients_by_path(registry)
        clients["nb/a.ipynb"].emit("cell_created")
        clients["nb/b.ipynb"].emit("source_changed")
        filtered = await server.poll_events(notebook="nb/b.ipynb")
        return filtered, await server.poll_events(cursor=filtered["cursor"])

    filtered, after = asyncio.run(scenario())
    assert [e["name"] for e in filtered["events"]] == ["source_changed"]
    assert after["events"] == []


def test_polling_the_same_events_twice_does_not_corrupt_their_truncation(monkeypatch):
    # The cleaners mutate outputs in place and the events stay in the log, so handing them the
    # stored payload made a second poll re-truncate and understate what was dropped.
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb")]})

    async def scenario():
        await registry.attach("nb/a.ipynb")
        outputs = [{"output_type": "stream", "text": "z" * 10000}]
        _clients_by_path(registry)["nb/a.ipynb"].emit("cell_executed", {"outputs": outputs})
        return await server.poll_events(), await server.poll_events()

    first, second = asyncio.run(scenario())
    assert first["events"][0]["data"]["outputs"][0]["text"] == second["events"][0]["data"]["outputs"][0]["text"]
    assert f"{10000 - _OUTPUT_CHAR_LIMIT} more chars" in first["events"][0]["data"]["outputs"][0]["text"]


def test_poll_events_can_filter_a_notebook_that_has_since_detached(monkeypatch):
    # The events outlive the attachment, so the filter that names them has to as well.
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb"), _record("nb/b.ipynb")]})

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")
        _clients_by_path(registry)["nb/a.ipynb"].emit("cell_created")
        await registry.detach(Attachment(DEFAULT_URL, "nb/a.ipynb"))
        return await server.poll_events(notebook="nb/a.ipynb")

    assert [e["name"] for e in asyncio.run(scenario())["events"]] == ["cell_created"]


def test_poll_events_reports_events_that_aged_out(monkeypatch):
    import nbclassic_mcp_bridge_mcp.registry as registry_module

    monkeypatch.setattr(registry_module, "EVENT_LOG_MAXLEN", 2)
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb")]})

    async def scenario():
        await registry.attach("nb/a.ipynb")
        client = _clients_by_path(registry)["nb/a.ipynb"]
        for _ in range(5):
            client.emit("cell_created")
        return await server.poll_events()

    result = asyncio.run(scenario())
    assert len(result["events"]) == 2
    assert result["dropped"] == 3


def test_poll_events_omits_dropped_when_nothing_was_lost(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb")]})

    async def scenario():
        await registry.attach("nb/a.ipynb")
        _clients_by_path(registry)["nb/a.ipynb"].emit("cell_created")
        return await server.poll_events()

    assert "dropped" not in asyncio.run(scenario())


@pytest.mark.parametrize(
    "filter_name, expected",
    [("nb/zzz.ipynb", "no retained events"), ("report", "matches several notebooks")],
    ids=["no-match", "ambiguous"],
)
def test_poll_events_rejects_a_filter_it_cannot_resolve(monkeypatch, filter_name, expected):
    # The poll filter resolves against a different candidate set than the tools do, so its own
    # failure branches need covering -- they are where the two resolvers drift apart.
    server, registry = _with_registry(
        monkeypatch, {DEFAULT_URL: [_record("a/report.ipynb"), _record("b/report.ipynb")]}
    )

    async def scenario():
        await registry.attach("a/report.ipynb")
        await registry.attach("b/report.ipynb")
        await server.poll_events(notebook=filter_name)

    with pytest.raises(RuntimeError, match=expected):
        asyncio.run(scenario())


def test_poll_events_does_not_retarget_the_current_notebook(monkeypatch):
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb"), _record("nb/b.ipynb")]})

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")
        await server.poll_events(notebook="nb/a.ipynb")

    asyncio.run(scenario())
    assert registry.is_current(Attachment(DEFAULT_URL, "nb/b.ipynb"))


def _with_offset_default(monkeypatch):
    """Attach on one server, then aim the default at another, so the two genuinely differ."""
    server, registry = _with_registry(monkeypatch, {DEFAULT_URL: [_record("nb/a.ipynb")]})
    asyncio.run(registry.attach("nb/a.ipynb"))
    registry.retarget(OTHER_URL, OTHER_TOKEN)
    return server, registry


def test_checkpoint_uses_the_attached_notebooks_own_endpoint(monkeypatch):
    # A notebook stays attached to the server it was attached against, so checkpointing has to
    # talk to that server rather than wherever the registry currently points.
    server, _ = _with_offset_default(monkeypatch)
    calls = []

    async def create(url, token, path):
        calls.append((url, token, path))
        return {"id": "cp1"}

    monkeypatch.setattr(server.discovery, "create_checkpoint", create)
    assert asyncio.run(server.checkpoint_notebook()) == {"id": "cp1", "notebook": "nb/a.ipynb"}
    assert calls == [(DEFAULT_URL, TOKEN, "nb/a.ipynb")]


def test_restore_uses_the_attached_notebooks_own_endpoint(monkeypatch):
    server, registry = _with_offset_default(monkeypatch)
    seen = []

    async def list_checkpoints(url, token, path):
        seen.append(("list", url, token, path))
        return [{"id": "cp1"}]

    async def restore(url, token, path, checkpoint_id):
        seen.append(("restore", url, token, path))

    monkeypatch.setattr(server.discovery, "list_checkpoints", list_checkpoints)
    monkeypatch.setattr(server.discovery, "restore_checkpoint", restore)
    message = asyncio.run(server.restore_notebook_checkpoint())
    assert seen == [
        ("list", DEFAULT_URL, TOKEN, "nb/a.ipynb"),
        ("restore", DEFAULT_URL, TOKEN, "nb/a.ipynb"),
    ]
    assert registry.current().commands == [("reload_notebook", {})]
    assert "restored nb/a.ipynb to checkpoint cp1" in message


def test_checkpoint_without_an_attachment_points_at_use_notebook(monkeypatch):
    server, _ = _with_registry(monkeypatch, {})
    with pytest.raises(RuntimeError, match="use_notebook first"):
        asyncio.run(server.checkpoint_notebook())


def test_restore_without_a_checkpoint_gives_an_actionable_error(monkeypatch):
    server, _ = _with_offset_default(monkeypatch)

    async def no_checkpoints(url, token, path):
        return []

    monkeypatch.setattr(server.discovery, "list_checkpoints", no_checkpoints)
    with pytest.raises(RuntimeError, match="checkpoint_notebook first"):
        asyncio.run(server.restore_notebook_checkpoint())
