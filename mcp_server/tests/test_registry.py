import asyncio

import pytest

from nbclassic_mcp_bridge_mcp.registry import Attachment, NotebookRegistry


DEFAULT_URL = "http://a:8888"
TOKEN = "tok-a"
OTHER_URL = "http://b:9999"
OTHER_TOKEN = "tok-b"

# A path no client can dial, standing in for a stopped server or a rejected token. Clients built
# inside attach cannot be flagged beforehand, so the failure has to ride on the path; an existing
# client is flagged directly with connect_fails.
UNREACHABLE = "nb/unreachable.ipynb"


class FakeRelayClient:
    """Relay client double: records the endpoint it was built for and every connect and close."""

    def __init__(self, jupyter_url, token, on_event=None):
        self.jupyter_url = jupyter_url
        self.token = token
        self.on_event = on_event
        self.notebook = None
        self.connects: list[str] = []
        self.closes = 0
        self.connect_fails = False
        self.close_fails = False

    def emit(self, name):
        """Deliver one event the way the read loop does, once it has cleared replay dedup."""
        self.on_event({"kind": "event", "name": name, "data": {}})

    async def connect(self, path):
        if path == UNREACHABLE or self.connect_fails:
            raise ConnectionError("jupyter server refused the websocket")
        self.connects.append(path)
        self.notebook = path

    async def close(self):
        self.closes += 1
        if self.close_fails:
            raise ConnectionError("socket teardown failed")


def build(url=DEFAULT_URL, token=TOKEN):
    """Return a registry backed by FakeRelayClients, plus the list of clients it has built."""
    created: list[FakeRelayClient] = []

    def factory(jupyter_url, token, on_event=None):
        client = FakeRelayClient(jupyter_url, token, on_event)
        created.append(client)
        return client

    return NotebookRegistry(url, token, client_factory=factory), created


def test_attach_connects_and_becomes_current():
    registry, created = build()
    client = asyncio.run(registry.attach("nb/a.ipynb"))
    assert len(created) == 1
    assert client.connects == ["nb/a.ipynb"]
    assert (client.jupyter_url, client.token) == (DEFAULT_URL, TOKEN)
    assert registry.current() is client


def test_current_without_an_attachment_points_at_use_notebook():
    registry, _ = build()
    with pytest.raises(RuntimeError, match="use_notebook first"):
        registry.current()


def test_attaching_another_notebook_leaves_the_first_attached():
    registry, created = build()
    attach(registry, "nb/a.ipynb", "nb/b.ipynb")
    first, second = created
    assert (first.closes, second.closes) == (0, 0)
    assert registry.attachments() == [
        Attachment(DEFAULT_URL, "nb/a.ipynb"),
        Attachment(DEFAULT_URL, "nb/b.ipynb"),
    ]
    assert registry.current() is second


def test_a_failed_attach_changes_nothing():
    # Nothing is registered until the connect succeeds, so a stopped server leaves the notebook
    # already in use attached and current.
    registry, created = build()

    async def scenario():
        await registry.attach("nb/a.ipynb")
        with pytest.raises(ConnectionError):
            await registry.attach(UNREACHABLE)

    asyncio.run(scenario())
    # The client built for the failed attach is discarded rather than registered.
    assert len(created) == 2
    assert created[0].closes == 0
    assert registry.attachments() == [Attachment(DEFAULT_URL, "nb/a.ipynb")]
    assert registry.current() is created[0]


def test_a_failed_reattach_keeps_the_notebook_attached():
    # The existing client survives a failed reconnect; command() dials it again on the next call.
    registry, created = build()

    async def scenario():
        await registry.attach("nb/a.ipynb")
        created[0].connect_fails = True
        with pytest.raises(ConnectionError):
            await registry.attach("nb/a.ipynb")

    asyncio.run(scenario())
    assert created[0].closes == 0
    assert registry.current() is created[0]


def test_reattaching_the_same_notebook_reuses_its_client():
    # A repeated use_notebook must not discard the event log or the replay position, so the
    # client is reconnected rather than replaced.
    registry, created = build()

    attach(registry, "nb/a.ipynb", "nb/a.ipynb")
    assert len(created) == 1
    assert created[0].connects == ["nb/a.ipynb", "nb/a.ipynb"]
    assert created[0].closes == 0


def test_the_same_path_on_two_servers_is_two_attachments():
    registry, created = build()

    attach(registry, "nb/a.ipynb")
    registry.retarget(OTHER_URL, OTHER_TOKEN)
    attach(registry, "nb/a.ipynb")
    assert len(created) == 2
    assert created[0].closes == 0
    assert (created[1].jupyter_url, created[1].token) == (OTHER_URL, OTHER_TOKEN)
    assert registry.attachments() == [
        Attachment(DEFAULT_URL, "nb/a.ipynb"),
        Attachment(OTHER_URL, "nb/a.ipynb"),
    ]
    assert registry.current() is created[1]


def test_retarget_moves_the_default_endpoint_without_detaching():
    registry, created = build()

    attach(registry, "nb/a.ipynb")
    registry.retarget(OTHER_URL, OTHER_TOKEN)
    assert created[0].closes == 0
    assert (registry.jupyter_url, registry.token) == (OTHER_URL, OTHER_TOKEN)
    assert registry.current() is created[0]


def test_endpoints_lead_with_the_default_and_dedupe():
    registry, _ = build()

    attach(registry, "nb/a.ipynb", "nb/b.ipynb")
    registry.retarget(OTHER_URL, OTHER_TOKEN)
    assert registry.endpoints() == [(OTHER_URL, OTHER_TOKEN), (DEFAULT_URL, TOKEN)]


def test_label_names_the_path_and_qualifies_only_on_a_collision():
    registry, _ = build()

    attach(registry, "nb/a.ipynb", "nb/b.ipynb")
    on_a = Attachment(DEFAULT_URL, "nb/a.ipynb")
    assert registry.label(on_a) == "nb/a.ipynb"

    registry.retarget(OTHER_URL, OTHER_TOKEN)
    attach(registry, "nb/a.ipynb")
    assert registry.label(on_a) == f"nb/a.ipynb on {DEFAULT_URL}"
    assert registry.label(Attachment(DEFAULT_URL, "nb/b.ipynb")) == "nb/b.ipynb"


def test_find_matches_loosely_and_reports_every_tie():
    registry, _ = build()

    attach(registry, "notebooks/2026/analysis.ipynb", "archive/analysis.ipynb", "nb/other.ipynb")
    assert registry.find("nb/other.ipynb") == [Attachment(DEFAULT_URL, "nb/other.ipynb")]
    assert registry.find("2026/analysis") == [Attachment(DEFAULT_URL, "notebooks/2026/analysis.ipynb")]
    assert {attachment.path for attachment in registry.find("analysis.ipynb")} == {
        "notebooks/2026/analysis.ipynb",
        "archive/analysis.ipynb",
    }
    assert registry.find("nothing-like-this") == []


def test_make_current_refuses_an_unattached_notebook():
    registry, _ = build()
    asyncio.run(registry.attach("nb/a.ipynb"))
    with pytest.raises(RuntimeError, match="not attached"):
        registry.make_current(Attachment(DEFAULT_URL, "nb/zzz.ipynb"))


def test_detach_drops_one_notebook_and_leaves_the_rest():
    registry, created = build()

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")
        await registry.detach(Attachment(DEFAULT_URL, "nb/a.ipynb"))

    asyncio.run(scenario())
    assert (created[0].closes, created[1].closes) == (1, 0)
    assert registry.attachments() == [Attachment(DEFAULT_URL, "nb/b.ipynb")]
    assert registry.current() is created[1]


def test_detaching_the_current_notebook_leaves_no_current():
    # No silent promotion: the next command says there is no current notebook and names what is
    # still attached, rather than acting on one nobody chose.
    registry, _ = build()

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")
        await registry.detach(Attachment(DEFAULT_URL, "nb/b.ipynb"))

    asyncio.run(scenario())
    with pytest.raises(RuntimeError, match=r"no current notebook; attached: nb/a\.ipynb"):
        registry.current()


def test_detaching_an_unattached_notebook_raises():
    registry, _ = build()
    asyncio.run(registry.attach("nb/a.ipynb"))
    with pytest.raises(RuntimeError, match="not attached"):
        asyncio.run(registry.detach(Attachment(DEFAULT_URL, "nb/zzz.ipynb")))


def _names(recorded):
    return [(attachment.path, event["name"]) for attachment, event in recorded]


def attach(registry, *paths):
    """Attach each path in turn, for tests whose interesting part is not the attaching."""

    async def scenario():
        for path in paths:
            await registry.attach(path)

    asyncio.run(scenario())


def test_events_from_several_notebooks_merge_in_arrival_order():
    registry, created = build()
    attach(registry, "nb/a.ipynb", "nb/b.ipynb")
    first, second = created
    first.emit("cell_created")
    second.emit("source_changed")
    first.emit("cell_deleted")

    recorded, cursor = registry.events_since(0)
    assert _names(recorded) == [
        ("nb/a.ipynb", "cell_created"),
        ("nb/b.ipynb", "source_changed"),
        ("nb/a.ipynb", "cell_deleted"),
    ]
    assert cursor == 3


def test_a_cursor_excludes_what_it_has_already_seen_across_notebooks():
    registry, created = build()
    attach(registry, "nb/a.ipynb", "nb/b.ipynb")
    created[0].emit("cell_created")
    _, cursor = registry.events_since(0)
    created[1].emit("cell_deleted")

    recorded, _ = registry.events_since(cursor)
    assert _names(recorded) == [("nb/b.ipynb", "cell_deleted")]


def test_a_filtered_poll_still_advances_past_the_events_it_skipped():
    # The cursor tracks the merged stream, not the filter -- otherwise a filtered poll would
    # silently re-deliver every other notebook's events on the next unfiltered one.
    registry, created = build()
    attach(registry, "nb/a.ipynb", "nb/b.ipynb")
    created[0].emit("cell_created")
    created[1].emit("source_changed")

    only_b = Attachment(DEFAULT_URL, "nb/b.ipynb")
    recorded, cursor = registry.events_since(0, only_b)
    assert _names(recorded) == [("nb/b.ipynb", "source_changed")]
    assert cursor == 2
    assert registry.events_since(cursor)[0] == []


def test_events_outlive_the_detach_of_the_notebook_that_raised_them():
    # They happened; dropping them would make the cursor skip back over events already acted on.
    registry, created = build()

    async def scenario():
        await registry.attach("nb/a.ipynb")
        created[0].emit("cell_created")
        await registry.detach(Attachment(DEFAULT_URL, "nb/a.ipynb"))

    asyncio.run(scenario())
    assert _names(registry.events_since(0)[0]) == [("nb/a.ipynb", "cell_created")]


def test_the_merged_log_is_bounded(monkeypatch):
    # One budget shared by every attachment, so a busy notebook can age out a quiet one's events.
    import nbclassic_mcp_bridge_mcp.registry as registry_module

    monkeypatch.setattr(registry_module, "EVENT_LOG_MAXLEN", 3)
    registry, created = build()
    attach(registry, "nb/a.ipynb")
    for index in range(5):
        created[0].emit(f"cell_created_{index}")

    recorded, cursor = registry.events_since(0)
    assert [name for _, name in _names(recorded)] == ["cell_created_2", "cell_created_3", "cell_created_4"]
    assert cursor == 5


def test_polling_before_attaching_yields_nothing_instead_of_raising():
    registry, _ = build()
    assert registry.events_since(7) == ([], 7)


def test_a_stale_cursor_recovers_instead_of_hiding_events():
    # An MCP restart empties the log while the assistant still holds its old, larger cursor; the
    # poll must come back empty with a usable cursor rather than hiding every later event.
    registry, created = build()
    asyncio.run(registry.attach("nb/a.ipynb"))
    created[0].emit("cell_created")

    stale, recovered = registry.events_since(10_000)
    assert stale == []
    assert recovered == 1
    assert _names(registry.events_since(0)[0]) == [("nb/a.ipynb", "cell_created")]


def test_a_failing_close_still_detaches():
    # The client is dropped before its teardown is awaited, so a close that raises cannot leave
    # the registry still holding a client whose socket is half gone.
    registry, created = build()

    async def scenario():
        await registry.attach("nb/a.ipynb")
        created[0].close_fails = True
        with pytest.raises(ConnectionError):
            await registry.detach(Attachment(DEFAULT_URL, "nb/a.ipynb"))

    asyncio.run(scenario())
    assert registry.attachments() == []
    with pytest.raises(RuntimeError, match="use_notebook first"):
        registry.current()
