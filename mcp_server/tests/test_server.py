import asyncio
import hashlib

import pytest

from nbclassic_mcp_bridge_mcp.server import (
    _JUPYTER_URL,
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


class StubRegistry:
    """Registry double for tool-level tests: records attachments, scripts tab presence."""

    def __init__(self, tab_connected=True):
        self.jupyter_url = "http://localhost:8888"
        self.token = "tok"
        self.connected_to = None
        self.notebook = None
        self._tab_connected = tab_connected

    async def attach(self, path):
        self.connected_to = path
        self.notebook = path
        return self

    def current(self):
        if self.notebook is None:
            raise RuntimeError("not attached -- call use_notebook first")
        return self

    async def extension_present(self, timeout=0.5):
        return self._tab_connected


def _run_use_notebook(monkeypatch, notebooks, path=None, tab_connected=True):
    import nbclassic_mcp_bridge_mcp.server as server

    stub = StubRegistry(tab_connected)
    monkeypatch.setattr(server, "_registry", stub)

    async def fake_open_notebooks():
        return notebooks

    monkeypatch.setattr(server, "_open_notebooks", fake_open_notebooks)
    return asyncio.run(server.use_notebook(path)), stub


def _record(path, tab=True):
    return {"path": path, "kernel_state": "idle", "browser_tab_connected": tab, "assistant_attached": False}


def test_use_notebook_auto_attaches_the_only_open_notebook(monkeypatch):
    message, stub = _run_use_notebook(monkeypatch, [_record("nb/a.ipynb")])
    assert stub.connected_to == "nb/a.ipynb"
    assert "attached to nb/a.ipynb" in message and "browser tab connected" in message


def test_use_notebook_prefers_the_notebook_with_a_live_tab(monkeypatch):
    notebooks = [_record("nb/a.ipynb", tab=False), _record("nb/b.ipynb", tab=True)]
    _, stub = _run_use_notebook(monkeypatch, notebooks)
    assert stub.connected_to == "nb/b.ipynb"


def test_use_notebook_lists_options_instead_of_guessing(monkeypatch):
    notebooks = [_record("nb/a.ipynb"), _record("nb/b.ipynb")]
    message, stub = _run_use_notebook(monkeypatch, notebooks)
    assert stub.connected_to is None
    assert "nb/a.ipynb" in message and "nb/b.ipynb" in message


def test_use_notebook_resolves_a_fuzzy_path(monkeypatch):
    message, stub = _run_use_notebook(monkeypatch, [_record("notebooks/demo.ipynb")], path="demo.ipynb")
    assert stub.connected_to == "notebooks/demo.ipynb"
    assert "resolved from 'demo.ipynb'" in message


def test_use_notebook_reports_ambiguous_paths_without_attaching(monkeypatch):
    notebooks = [_record("a/report.ipynb"), _record("b/report.ipynb")]
    message, stub = _run_use_notebook(monkeypatch, notebooks, path="report.ipynb")
    assert stub.connected_to is None
    assert "a/report.ipynb" in message and "b/report.ipynb" in message


def test_use_notebook_attaches_verbatim_when_nothing_matches(monkeypatch):
    # The room outlives the call, so attaching before the human opens the notebook still works.
    message, stub = _run_use_notebook(monkeypatch, [_record("nb/a.ipynb")], path="new.ipynb", tab_connected=False)
    assert stub.connected_to == "new.ipynb"
    assert "no browser tab is connected" in message and "nb/a.ipynb" in message


class StubClient:
    """Attached-notebook double carrying its own endpoint, which need not be the registry default."""

    def __init__(self, notebook="nb/a.ipynb"):
        self.notebook = notebook
        self.jupyter_url = "http://other:9999"
        self.token = "tok-other"
        self.commands: list[tuple[str, dict]] = []

    async def command(self, op, args):
        self.commands.append((op, args))
        return {}


class StubEndpointRegistry:
    """Registry double whose default endpoint differs from its attached notebook's."""

    def __init__(self, client):
        self.jupyter_url = "http://localhost:8888"
        self.token = "tok-default"
        self._client = client

    def current(self):
        return self._client


def _with_client(monkeypatch):
    import nbclassic_mcp_bridge_mcp.server as server

    client = StubClient()
    monkeypatch.setattr(server, "_registry", StubEndpointRegistry(client))
    return server, client


def test_checkpoint_uses_the_attached_notebooks_own_endpoint(monkeypatch):
    # A notebook stays attached to the server it was attached against, so checkpointing has to
    # talk to that server rather than wherever the registry currently points.
    server, _ = _with_client(monkeypatch)
    calls = []

    async def create(url, token, path):
        calls.append((url, token, path))
        return {"id": "cp1"}

    monkeypatch.setattr(server.discovery, "create_checkpoint", create)
    assert asyncio.run(server.checkpoint_notebook()) == {"id": "cp1"}
    assert calls == [("http://other:9999", "tok-other", "nb/a.ipynb")]


def test_restore_uses_the_attached_notebooks_own_endpoint(monkeypatch):
    server, client = _with_client(monkeypatch)
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
        ("list", "http://other:9999", "tok-other", "nb/a.ipynb"),
        ("restore", "http://other:9999", "tok-other", "nb/a.ipynb"),
    ]
    assert client.commands == [("reload_notebook", {})]
    assert "restored nb/a.ipynb to checkpoint cp1" in message


def test_checkpoint_without_an_attachment_points_at_use_notebook(monkeypatch):
    import nbclassic_mcp_bridge_mcp.server as server

    monkeypatch.setattr(server, "_registry", StubRegistry())
    with pytest.raises(RuntimeError, match="use_notebook first"):
        asyncio.run(server.checkpoint_notebook())


def test_restore_without_a_checkpoint_gives_an_actionable_error(monkeypatch):
    import nbclassic_mcp_bridge_mcp.server as server

    stub = StubRegistry()
    stub.notebook = "nb/a.ipynb"
    monkeypatch.setattr(server, "_registry", stub)

    async def no_checkpoints(url, token, path):
        return []

    monkeypatch.setattr(server.discovery, "list_checkpoints", no_checkpoints)
    with pytest.raises(RuntimeError, match="checkpoint_notebook first"):
        asyncio.run(server.restore_notebook_checkpoint())
