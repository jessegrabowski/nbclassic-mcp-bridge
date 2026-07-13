import json
from collections import deque
from uuid import uuid4


PROTOCOL_VERSION = 0
ROLES = ("extension", "mcp")

# Cap on buffered events per room; the oldest fall off and can no longer be replayed.
EVENT_BUFFER_MAXLEN = 1000


def _other(role):
    return "mcp" if role == "extension" else "extension"


def _send(peer, msg):
    peer.send(json.dumps(msg))


class Switchboard:
    """Transport-agnostic relay logic for the nbclassic-mcp-bridge.

    Operates on duck-typed *peers*: any object with mutable ``role`` and ``notebook`` attributes
    (both ``None`` until a successful ``hello``) and ``send(text)`` / ``close(code, reason)``
    methods. ``BridgeHandler`` is the production peer; tests use a list-backed fake.

    Events from the extension are stamped with a per-room sequence number, retained in a bounded
    buffer, and replayed to an mcp peer that joins (or rejoins) past its declared position.

    All methods are synchronous: ``send`` is fire-and-forget and ``close`` only schedules a close,
    so nothing here needs to await.
    """

    def __init__(self):
        # notebook path -> {role -> peer}. A room pairs the browser
        # ("extension") with the assistant ("mcp").
        self.rooms: dict[str, dict[str, object]] = {}
        # Identifies this switchboard's event numbering: a client's last_event_seq is only
        # meaningful against the log_id it was learned from, so a relay restart (fresh, empty
        # buffers) cannot silently swallow newly numbered events.
        self.log_id = uuid4().hex[:8]
        self._event_buffers: dict[str, deque] = {}
        self._event_seqs: dict[str, int] = {}

    def route(self, peer, raw):
        """Handle one inbound frame from ``peer``."""
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            peer.close(1003, "expected a JSON text frame")
            return
        if not isinstance(msg, dict):
            peer.close(1003, "expected a JSON object frame")
            return

        kind = msg.get("kind")
        if kind == "hello":
            self._hello(peer, msg)
            return
        if peer.role is None:
            peer.close(1002, "send a hello frame before anything else")
            return

        if kind == "event" and peer.role == "extension":
            raw = self._stamp_and_buffer(peer.notebook, msg)

        target = self._peer(peer)
        if target is not None:
            target.send(raw)
        elif kind == "cmd":
            # The other side isn't connected -- answer the command ourselves so
            # the caller does not wait forever.
            _send(
                peer,
                {
                    "kind": "reply",
                    "id": msg.get("id"),
                    "ok": False,
                    "error": f"no {_other(peer.role)} peer connected",
                },
            )
        # reply / event / status frames to an absent peer are dropped.

    def leave(self, peer):
        """Handle ``peer`` disconnecting."""
        room = self.rooms.get(peer.notebook)
        if room is None or room.get(peer.role) is not peer:
            # Never finished a hello, or already evicted by a newer connection.
            return
        del room[peer.role]
        target = self._peer(peer)
        if target is not None:
            _send(target, {"kind": "status", "peer": peer.role, "state": "left"})
        if not room:
            del self.rooms[peer.notebook]
            self._event_buffers.pop(peer.notebook, None)
            self._event_seqs.pop(peer.notebook, None)

    def _hello(self, peer, msg):
        if msg.get("protocol") != PROTOCOL_VERSION:
            peer.close(1002, f"protocol mismatch: relay speaks v{PROTOCOL_VERSION}")
            return
        role = msg.get("role")
        notebook = msg.get("notebook")
        if role not in ROLES or not isinstance(notebook, str):
            peer.close(1002, "hello needs role in {extension, mcp} and a notebook path")
            return
        if peer.role is not None:
            peer.close(1002, "duplicate hello")
            return

        peer.role = role
        peer.notebook = notebook
        peer.capabilities = msg.get("capabilities")
        room = self.rooms.setdefault(notebook, {})

        # Reconnect or a second browser tab: evict the stale peer in this role.
        existing = room.get(role)
        if existing is not None and existing is not peer:
            existing.close(1001, "replaced by a newer connection")

        room[role] = peer

        target = self._peer(peer)
        if target is not None:
            # Tell each side that the other is present, carrying the extension's declared
            # capabilities so the mcp side can pre-check its commands.
            _send(target, {"kind": "status", **self._presence_frame(role, "joined", peer)})
            _send(peer, {"kind": "status", **self._presence_frame(_other(role), "joined", target)})

        if role == "mcp":
            self._replay_events(peer, msg)

    def _presence_frame(self, role, state, peer):
        frame = {"peer": role, "state": state}
        if role == "extension" and getattr(peer, "capabilities", None) is not None:
            frame["capabilities"] = peer.capabilities
        return frame

    def _stamp_and_buffer(self, notebook, msg):
        """Number the event, retain it for replay, and return the frame to forward."""
        seq = self._event_seqs.get(notebook, 0) + 1
        self._event_seqs[notebook] = seq
        msg["seq"] = seq
        msg["log_id"] = self.log_id
        buffer = self._event_buffers.get(notebook)
        if buffer is None:
            buffer = self._event_buffers[notebook] = deque(maxlen=EVENT_BUFFER_MAXLEN)
        buffer.append(msg)
        return json.dumps(msg)

    def _replay_events(self, peer, hello):
        """Resend buffered events the joining mcp peer has not seen.

        The peer's ``last_event_seq`` only counts if it was learned from this switchboard's
        ``log_id``; otherwise everything buffered for the room is replayed.
        """
        buffered = self._event_buffers.get(peer.notebook)
        if not buffered:
            return
        last_seen = hello.get("last_event_seq", 0) if hello.get("log_id") == self.log_id else 0
        if not isinstance(last_seen, int):
            last_seen = 0
        for event in buffered:
            if event["seq"] > last_seen:
                _send(peer, event)

    def presence(self):
        """Map each notebook path with a live room to the sorted roles present in it."""
        return {notebook: sorted(room) for notebook, room in self.rooms.items() if room}

    def _peer(self, peer):
        return self.rooms.get(peer.notebook, {}).get(_other(peer.role))
