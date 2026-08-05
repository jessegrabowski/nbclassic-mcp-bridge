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

    def __init__(self, jupyter_url, token):
        self.jupyter_url = jupyter_url
        self.token = token
        self.notebook = None
        self.connects: list[str] = []
        self.closes = 0
        self.events: list[dict] = []
        self.connect_fails = False
        self.close_fails = False

    async def connect(self, path):
        if path == UNREACHABLE or self.connect_fails:
            raise ConnectionError("jupyter server refused the websocket")
        self.connects.append(path)
        self.notebook = path

    async def close(self):
        self.closes += 1
        if self.close_fails:
            raise ConnectionError("socket teardown failed")

    def events_since(self, cursor):
        return self.events[cursor:], len(self.events)


def build(url=DEFAULT_URL, token=TOKEN):
    """Return a registry backed by FakeRelayClients, plus the list of clients it has built."""
    created: list[FakeRelayClient] = []

    def factory(jupyter_url, token):
        client = FakeRelayClient(jupyter_url, token)
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

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")

    asyncio.run(scenario())
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

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/a.ipynb")

    asyncio.run(scenario())
    assert len(created) == 1
    assert created[0].connects == ["nb/a.ipynb", "nb/a.ipynb"]
    assert created[0].closes == 0


def test_the_same_path_on_two_servers_is_two_attachments():
    registry, created = build()

    async def scenario():
        await registry.attach("nb/a.ipynb")
        registry.retarget(OTHER_URL, OTHER_TOKEN)
        await registry.attach("nb/a.ipynb")

    asyncio.run(scenario())
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

    async def scenario():
        await registry.attach("nb/a.ipynb")
        registry.retarget(OTHER_URL, OTHER_TOKEN)

    asyncio.run(scenario())
    assert created[0].closes == 0
    assert (registry.jupyter_url, registry.token) == (OTHER_URL, OTHER_TOKEN)
    assert registry.current() is created[0]


def test_endpoints_lead_with_the_default_and_dedupe():
    registry, _ = build()

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")
        registry.retarget(OTHER_URL, OTHER_TOKEN)

    asyncio.run(scenario())
    assert registry.endpoints() == [(OTHER_URL, OTHER_TOKEN), (DEFAULT_URL, TOKEN)]


def test_label_names_the_path_and_qualifies_only_on_a_collision():
    registry, _ = build()

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")

    asyncio.run(scenario())
    on_a = Attachment(DEFAULT_URL, "nb/a.ipynb")
    assert registry.label(on_a) == "nb/a.ipynb"

    async def collide():
        registry.retarget(OTHER_URL, OTHER_TOKEN)
        await registry.attach("nb/a.ipynb")

    asyncio.run(collide())
    assert registry.label(on_a) == f"nb/a.ipynb on {DEFAULT_URL}"
    assert registry.label(Attachment(DEFAULT_URL, "nb/b.ipynb")) == "nb/b.ipynb"


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


def test_events_come_from_the_attached_notebook():
    registry, created = build()
    asyncio.run(registry.attach("nb/a.ipynb"))
    created[0].events = [{"name": "cell_created"}, {"name": "cell_deleted"}]
    assert registry.events_since(1) == ([{"name": "cell_deleted"}], 2)


def test_polling_before_attaching_yields_nothing_instead_of_raising():
    registry, _ = build()
    assert registry.events_since(7) == ([], 7)


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
