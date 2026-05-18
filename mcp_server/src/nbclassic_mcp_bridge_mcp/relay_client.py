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

    Holds one connection to the relay for the attached notebook. ``command``
    issues a ``cmd`` frame and awaits the matching ``reply``; ``_read_loop``
    resolves those replies and files unsolicited ``event`` frames (the human's
    edits) into a bounded log that ``events_since`` drains.
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
        await self.close()
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

    async def close(self) -> None:
        """Tear down the connection and fail any in-flight commands."""
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
        self._fail_pending(ConnectionError("relay connection closed"))

    async def command(self, op: str, args: dict) -> dict:
        """Send a ``cmd`` and return the ``result`` of the matching ``reply``.

        Raise RuntimeError if not connected or if the extension reports failure.
        """
        if self._ws is None:
            raise RuntimeError("not connected -- call use_notebook first")
        msg_id = next(self._ids)
        log.debug("cmd %s id=%s", op, msg_id)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send(json.dumps({"kind": "cmd", "id": msg_id, "op": op, "args": args}))
        reply = await fut
        if not reply.get("ok"):
            error = reply.get("error", "relay command failed")
            log.warning("cmd %s id=%s failed: %s", op, msg_id, error)
            raise RuntimeError(error)
        return reply.get("result")

    def events_since(self, cursor: int) -> tuple[list[dict], int]:
        """Return events logged after ``cursor`` and the cursor to pass next.

        ``cursor`` is the value returned by the previous call, or 0 to start.
        """
        fresh = [event for seq, event in self._event_log if seq > cursor]
        latest = self._event_log[-1][0] if self._event_log else cursor
        return fresh, latest

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
                # status frames carry no state the client acts on; ignore them.
        except websockets.ConnectionClosed:
            # The relay closed the socket -- normal read-loop termination.
            pass
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
