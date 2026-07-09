import hashlib

from nbclassic_mcp_bridge_mcp.server import (
    _JUPYTER_URL,
    _cell_output_view,
    _cell_source_view,
    _clean_output,
    _derive_endpoint,
    _outline_cell,
    _summarize_output,
    _truncate,
)


def test_derive_endpoint_matches_nb_token(tmp_path):
    # nb-token hashes the physical path: sha256(`cd DIR && pwd -P`) as 64 hex chars.
    url, token = _derive_endpoint(str(tmp_path))
    assert url == _JUPYTER_URL
    assert token == hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()


def test_derive_endpoint_resolves_symlinks_like_nb_token(tmp_path):
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
