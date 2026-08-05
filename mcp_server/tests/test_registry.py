import asyncio

import pytest

from nbclassic_mcp_bridge_mcp.registry import NotebookRegistry


# Attaching to this path fails, standing in for a stopped server or a rejected token.
UNREACHABLE = "nb/unreachable.ipynb"


class FakeClient:
    """Relay client double: records the endpoint it was built for and every connect and close."""

    def __init__(self, jupyter_url, token):
        self.jupyter_url = jupyter_url
        self.token = token
        self.notebook = None
        self.connects: list[str] = []
        self.closes = 0
        self.events: list[dict] = []

    async def connect(self, path):
        if path == UNREACHABLE:
            raise ConnectionError("jupyter server refused the websocket")
        self.connects.append(path)
        self.notebook = path

    async def close(self):
        self.closes += 1

    def events_since(self, cursor):
        return self.events[cursor:], len(self.events)


def build(url="http://a:8888", token="tok-a"):
    """Return a registry backed by FakeClients, plus the list of clients it has built."""
    created: list[FakeClient] = []

    def factory(jupyter_url, token):
        client = FakeClient(jupyter_url, token)
        created.append(client)
        return client

    return NotebookRegistry(url, token, client_factory=factory), created


def test_attach_connects_and_becomes_current():
    registry, created = build()
    client = asyncio.run(registry.attach("nb/a.ipynb"))
    assert created == [client]
    assert client.connects == ["nb/a.ipynb"]
    assert (client.jupyter_url, client.token) == ("http://a:8888", "tok-a")
    assert registry.current() is client


def test_current_without_an_attachment_points_at_use_notebook():
    registry, _ = build()
    with pytest.raises(RuntimeError, match="use_notebook first"):
        registry.current()


def test_attaching_another_notebook_closes_the_previous_one():
    # The single-attachment policy: the relay would hold both rooms, but the tool surface
    # exposes one notebook at a time.
    registry, created = build()

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.attach("nb/b.ipynb")

    asyncio.run(scenario())
    first, second = created
    assert first.closes == 1
    assert second.closes == 0
    assert registry.current() is second


def test_a_failed_attach_reports_that_nothing_is_attached():
    # The previous notebook is dropped before the new one is dialed, so a connect failure must
    # leave every later call saying "call use_notebook first" rather than raising KeyError on a
    # client the registry no longer holds.
    registry, created = build()

    async def scenario():
        await registry.attach("nb/a.ipynb")
        with pytest.raises(ConnectionError):
            await registry.attach(UNREACHABLE)

    asyncio.run(scenario())
    assert created[0].closes == 1
    assert registry.events_since(0) == ([], 0)
    with pytest.raises(RuntimeError, match="use_notebook first"):
        registry.current()


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


def test_the_same_path_on_another_server_is_a_different_attachment():
    registry, created = build()

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.retarget("http://b:9999", "tok-b")
        await registry.attach("nb/a.ipynb")

    asyncio.run(scenario())
    assert len(created) == 2
    assert (created[1].jupyter_url, created[1].token) == ("http://b:9999", "tok-b")
    assert registry.current() is created[1]


def test_retarget_drops_attachments_and_moves_the_default_endpoint():
    registry, created = build()

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.retarget("http://b:9999", "tok-b")

    asyncio.run(scenario())
    assert created[0].closes == 1
    assert (registry.jupyter_url, registry.token) == ("http://b:9999", "tok-b")
    with pytest.raises(RuntimeError, match="use_notebook first"):
        registry.current()


def test_events_come_from_the_attached_notebook():
    registry, created = build()
    asyncio.run(registry.attach("nb/a.ipynb"))
    created[0].events = [{"name": "cell_created"}, {"name": "cell_deleted"}]
    assert registry.events_since(1) == ([{"name": "cell_deleted"}], 2)


def test_polling_before_attaching_yields_nothing_instead_of_raising():
    registry, _ = build()
    assert registry.events_since(7) == ([], 7)


def test_close_all_drops_every_client_and_clears_current():
    registry, created = build()

    async def scenario():
        await registry.attach("nb/a.ipynb")
        await registry.close_all()

    asyncio.run(scenario())
    assert created[0].closes == 1
    with pytest.raises(RuntimeError, match="use_notebook first"):
        registry.current()
