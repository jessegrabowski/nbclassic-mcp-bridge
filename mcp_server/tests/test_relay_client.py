import asyncio
import json

import pytest
import websockets

from nbclassic_mcp_bridge_mcp.relay_client import PROTOCOL_VERSION, RelayClient


# Sentinel for "accept the cmd but never reply" -- exercises the disconnect path.
DROP = object()


class FakeRelay:
    """A scriptable stand-in for the relay, exposed as an async context manager.

    Records every ``hello`` it receives, answers each ``cmd`` from the ``replies`` table (op ->
    reply spec, or ``DROP`` to stay silent), and pushes the frames in ``events`` on each handshake.
    """

    def __init__(self, replies=None, events=None, extension_joined=False):
        self.replies = replies or {}
        self.events = events or []
        self.extension_joined = extension_joined
        self.hellos: list[dict] = []
        self._server = None
        self._connections: set = set()
        self.port: int | None = None

    @property
    def jupyter_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def __aenter__(self):
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def kick(self):
        """Server-side close of every live connection (eviction / restart)."""
        for ws in list(self._connections):
            await ws.close(code=1001, reason="replaced by a newer connection")

    async def _handle(self, ws):
        self._connections.add(ws)
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg["kind"] == "hello":
                    self.hellos.append(msg)
                    if self.extension_joined:
                        await ws.send(json.dumps({"kind": "status", "peer": "extension", "state": "joined"}))
                    for name, data in self.events:
                        await ws.send(json.dumps({"kind": "event", "name": name, "data": data}))
                elif msg["kind"] == "cmd":
                    spec = self.replies.get(msg["op"], {"ok": True, "result": None})
                    if spec is DROP:
                        continue
                    await ws.send(json.dumps({"kind": "reply", "id": msg["id"], **spec}))
        finally:
            self._connections.discard(ws)


def test_connect_sends_a_well_formed_hello():
    async def scenario():
        async with FakeRelay() as relay:
            client = RelayClient(relay.jupyter_url, token="tok")
            await client.connect("nb.ipynb")
            await client.command("snapshot", {})  # round-trip barrier
            await client.close()
            return relay.hellos[-1]

    assert asyncio.run(scenario()) == {
        "kind": "hello",
        "protocol": PROTOCOL_VERSION,
        "role": "mcp",
        "notebook": "nb.ipynb",
    }


@pytest.mark.parametrize(
    "op, result",
    [
        ("snapshot", [{"cell_id": "a", "source": "x"}]),
        ("read_cell", {"cell_id": "a", "source": "x"}),
        ("delete_cell", {"cell_id": "a"}),
    ],
)
def test_command_returns_the_reply_result(op, result):
    async def scenario():
        async with FakeRelay(replies={op: {"ok": True, "result": result}}) as relay:
            client = RelayClient(relay.jupyter_url, "tok")
            await client.connect("nb.ipynb")
            got = await client.command(op, {})
            await client.close()
            return got

    assert asyncio.run(scenario()) == result


def test_command_raises_on_a_failure_reply():
    async def scenario():
        replies = {"delete_cell": {"ok": False, "error": "no such cell"}}
        async with FakeRelay(replies=replies) as relay:
            client = RelayClient(relay.jupyter_url, "tok")
            await client.connect("nb.ipynb")
            try:
                await client.command("delete_cell", {"cell_id": "z"})
            finally:
                await client.close()

    with pytest.raises(RuntimeError, match="no such cell"):
        asyncio.run(scenario())


def test_command_before_connect_raises():
    async def scenario():
        client = RelayClient("http://127.0.0.1:1", "tok")
        await client.command("snapshot", {})

    with pytest.raises(RuntimeError, match="not connected"):
        asyncio.run(scenario())


def test_events_are_logged_and_polled_with_a_cursor():
    async def scenario():
        events = [
            ("cell_created", {"cell_id": "a"}),
            ("source_changed", {"cell_id": "a", "source": "y"}),
        ]
        async with FakeRelay(events=events) as relay:
            client = RelayClient(relay.jupyter_url, "tok")
            await client.connect("nb.ipynb")
            await client.command("snapshot", {})  # barrier: events arrive first (FIFO)
            first, cursor = client.events_since(0)
            second, cursor_again = client.events_since(cursor)
            await client.close()
            return first, cursor, second, cursor_again

    first, cursor, second, cursor_again = asyncio.run(scenario())
    assert [e["name"] for e in first] == ["cell_created", "source_changed"]
    assert second == []
    assert cursor_again == cursor


def test_disconnect_fails_a_pending_command_instead_of_hanging():
    async def scenario():
        async with FakeRelay(replies={"snapshot": DROP}) as relay:
            client = RelayClient(relay.jupyter_url, "tok")
            await client.connect("nb.ipynb")
            pending = asyncio.create_task(client.command("snapshot", {}))
            await asyncio.sleep(0.05)  # let the cmd reach the silent relay
        # relay closed on context exit -> the awaiting command must error out,
        # flagging that the command's fate is unknown (no blind resend).
        await asyncio.wait_for(pending, timeout=2)

    with pytest.raises(RuntimeError, match="may not have been applied"):
        asyncio.run(scenario())


def test_reconnecting_attaches_to_the_new_notebook():
    async def scenario():
        async with FakeRelay(replies={"snapshot": {"ok": True, "result": "ok"}}) as relay:
            client = RelayClient(relay.jupyter_url, "tok")
            await client.connect("a.ipynb")
            await client.connect("b.ipynb")
            got = await client.command("snapshot", {})
            await client.close()
            return got, relay.hellos[-1]

    got, hello = asyncio.run(scenario())
    assert got == "ok"
    assert hello["notebook"] == "b.ipynb"


@pytest.mark.parametrize("joined", [True, False], ids=["extension-present", "extension-absent"])
def test_extension_present_reflects_the_relay_status_frame(joined):
    async def scenario():
        async with FakeRelay(extension_joined=joined) as relay:
            client = RelayClient(relay.jupyter_url, "tok")
            await client.connect("nb.ipynb")
            present = await client.extension_present(timeout=0.5)
            await client.close()
            return present

    assert asyncio.run(scenario()) is joined


def test_events_since_recovers_from_a_stale_cursor():
    # An MCP restart resets the event log while an agent may still hold its old, larger cursor;
    # the poll must come back empty with a usable cursor instead of hiding new events forever.
    async def scenario():
        events = [("cell_created", {"cell_id": "a"}), ("cell_deleted", {"cell_id": "a"})]
        async with FakeRelay(events=events) as relay:
            client = RelayClient(relay.jupyter_url, "tok")
            await client.connect("nb.ipynb")
            await client.command("snapshot", {})  # barrier: events arrive first (FIFO)
            stale, recovered_cursor = client.events_since(cursor=10_000)
            fresh, _ = client.events_since(0)
            await client.close()
            return stale, recovered_cursor, fresh

    stale, recovered_cursor, fresh = asyncio.run(scenario())
    assert stale == []
    assert recovered_cursor == 2
    assert [e["name"] for e in fresh] == ["cell_created", "cell_deleted"]


def test_command_reattaches_after_the_relay_drops_the_connection():
    async def scenario():
        async with FakeRelay(replies={"snapshot": {"ok": True, "result": "ok"}}) as relay:
            client = RelayClient(relay.jupyter_url, "tok")
            await client.connect("nb.ipynb")
            await client.command("snapshot", {})
            await relay.kick()  # eviction by another client, or a relay restart
            got = await client.command("snapshot", {})
            await client.close()
            return got, relay.hellos

    got, hellos = asyncio.run(scenario())
    assert got == "ok"
    assert [h["notebook"] for h in hellos] == ["nb.ipynb", "nb.ipynb"]


def test_large_reply_survives_the_websockets_size_limit():
    # A snapshot of a plot-heavy notebook runs to several MiB; the default
    # websockets max_size (1 MiB) would drop the connection mid-reply.
    big = "x" * (4 * 1024 * 1024)

    async def scenario():
        async with FakeRelay(replies={"snapshot": {"ok": True, "result": big}}) as relay:
            client = RelayClient(relay.jupyter_url, "tok")
            await client.connect("nb.ipynb")
            got = await client.command("snapshot", {})
            await client.close()
            return got

    assert asyncio.run(scenario()) == big
