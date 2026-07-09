import asyncio
import itertools
import json
import logging
from collections import deque
from urllib.parse import urlsplit, urlunsplit

import websockets


PROTOCOL_VERSION = 0

# Cap on retained events; older ones fall off the poll feed.
_EVENT_LOG_MAXLEN = 1000

# A notebook snapshot with image outputs runs to several MiB; lift the websockets
# client's 1 MiB default frame ceiling well past it.
_MAX_FRAME_BYTES = 128 * 1024 * 1024

log = logging.getLogger(__name__)


class RelayClient:
    """WebSocket client to the nbclassic-mcp-bridge relay (role = "mcp").

    Holds one connection to the relay for the attached notebook. ``command`` issues a ``cmd`` frame
    and awaits the matching ``reply``; ``_read_loop`` resolves those replies, files unsolicited
    ``event`` frames (the human's edits) into a bounded log that ``events_since`` drains, and tracks
    the extension peer's presence from ``status`` frames.
    """

    def __init__(self, jupyter_url: str, token: str):
        self._jupyter_url = jupyter_url
        self._token = token
        self._ws = None
        self._notebook: str | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._seq = itertools.count(1)
        self._event_log: deque[tuple[int, dict]] = deque(maxlen=_EVENT_LOG_MAXLEN)
        # Serializes connect/close/reattach so concurrent commands cannot tear down each other's sockets.
        self._conn_lock = asyncio.Lock()
        self._extension_joined = asyncio.Event()

    def _relay_url(self) -> str:
        """Derive the relay WebSocket URL from the configured Jupyter URL."""
        parts = urlsplit(self._jupyter_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        path = parts.path.rstrip("/") + "/mcp-bridge"
        query = f"token={self._token}" if self._token else ""
        return urlunsplit((scheme, parts.netloc, path, query, ""))

    async def connect(self, notebook: str) -> None:
        """Open the relay socket for ``notebook`` and start reading replies.

        Closes any prior connection first, so this doubles as a re-attach.
        """
        async with self._conn_lock:
            await self._teardown()
            await self._dial(notebook)

    async def close(self) -> None:
        """Tear down the connection and fail any in-flight commands."""
        async with self._conn_lock:
            await self._teardown()

    async def retarget(self, jupyter_url: str, token: str) -> None:
        """Point at a different Jupyter server; drops any current connection.

        The next ``connect`` opens the relay socket against the new endpoint.
        """
        async with self._conn_lock:
            await self._teardown()
            self._jupyter_url = jupyter_url
            self._token = token
            self._notebook = None

    async def extension_present(self, timeout: float = 0.5) -> bool:
        """Report whether the notebook's browser tab is connected to the relay.

        The relay announces a present extension peer immediately after the handshake, so a short
        wait distinguishes "not there" from "frame still in flight". Default timeout 0.5 seconds.
        """
        try:
            await asyncio.wait_for(self._extension_joined.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def command(self, op: str, args: dict) -> dict:
        """Send a ``cmd`` and return the ``result`` of the matching ``reply``.

        Reattach to the current notebook first if the relay connection dropped (server restart, or
        another MCP client took the room). Raise RuntimeError if never attached or the extension
        reports failure.
        """
        if self._notebook is None:
            raise RuntimeError("not connected -- call use_notebook first")
        await self._ensure_connected()
        try:
            fut = await self._send_cmd(op, args)
        except websockets.ConnectionClosed:
            # The frame never left the socket, so one reconnect-and-resend is safe even for mutations.
            log.info("relay connection dropped mid-send; reattaching to %s", self._notebook)
            await self.connect(self._notebook)
            fut = await self._send_cmd(op, args)
        try:
            reply = await fut
        except ConnectionError as exc:
            raise RuntimeError(
                f"relay connection lost while running {op}; the command may not have been applied"
            ) from exc
        if not reply.get("ok"):
            error = reply.get("error", "relay command failed")
            log.warning("cmd %s failed: %s", op, error)
            raise RuntimeError(error)
        return reply.get("result")

    def events_since(self, cursor: int) -> tuple[list[dict], int]:
        """Return events logged after ``cursor`` and the cursor to pass next.

        ``cursor`` is the value returned by the previous call, or 0 to start.
        """
        fresh = [event for seq, event in self._event_log if seq > cursor]
        latest = self._event_log[-1][0] if self._event_log else cursor
        return fresh, latest

    async def _dial(self, notebook: str) -> None:
        """Open the socket, send the hello, and start the reader. Caller holds the lock."""
        self._extension_joined.clear()
        self._ws = await websockets.connect(self._relay_url(), max_size=_MAX_FRAME_BYTES)
        await self._ws.send(
            json.dumps(
                {
                    "kind": "hello",
                    "protocol": PROTOCOL_VERSION,
                    "role": "mcp",
                    "notebook": notebook,
                }
            )
        )
        self._notebook = notebook
        self._reader_task = asyncio.create_task(self._read_loop())
        log.info("relay connected for notebook %s", notebook)

    async def _teardown(self) -> None:
        """Stop the reader, close the socket, and fail in-flight commands. Caller holds the lock."""
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        self._extension_joined.clear()
        self._fail_pending(ConnectionError("relay connection closed"))

    def _connection_lost(self) -> bool:
        """True when there is no live relay socket (never connected, or dropped)."""
        return self._ws is None or (self._reader_task is not None and self._reader_task.done())

    async def _ensure_connected(self) -> None:
        """Reattach to the current notebook if the connection dropped; no-op while healthy."""
        async with self._conn_lock:
            if not self._connection_lost():
                return
            log.info("relay connection down; reattaching to %s", self._notebook)
            await self._teardown()
            await self._dial(self._notebook)

    async def _send_cmd(self, op: str, args: dict) -> asyncio.Future:
        """Register a pending reply, send one ``cmd`` frame, return the reply future."""
        msg_id = next(self._ids)
        log.debug("cmd %s id=%s", op, msg_id)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self._ws.send(json.dumps({"kind": "cmd", "id": msg_id, "op": op, "args": args}))
        except websockets.ConnectionClosed:
            self._pending.pop(msg_id, None)
            raise
        return fut

    async def _read_loop(self) -> None:
        """Dispatch inbound frames until the socket closes."""
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                kind = msg.get("kind")
                if kind == "reply":
                    fut = self._pending.pop(msg.get("id"), None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                elif kind == "event":
                    self._event_log.append((next(self._seq), msg))
                elif kind == "status" and msg.get("peer") == "extension":
                    if msg.get("state") == "joined":
                        self._extension_joined.set()
                    else:
                        self._extension_joined.clear()
        except websockets.ConnectionClosed:
            # The relay closed the socket -- normal read-loop termination.
            pass
        except Exception:
            log.exception("relay read loop died on an unexpected error")
        finally:
            if self._pending:
                log.warning("relay connection lost with %d command(s) in flight", len(self._pending))
            else:
                log.info("relay connection closed")
            self._fail_pending(ConnectionError("relay connection lost"))

    def _fail_pending(self, exc: Exception) -> None:
        """Resolve every awaiting command with ``exc`` so callers never hang."""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()
