import json


PROTOCOL_VERSION = 0
ROLES = ("extension", "mcp")


def _other(role):
    return "mcp" if role == "extension" else "extension"


def _send(peer, msg):
    peer.send(json.dumps(msg))


class Switchboard:
    """Transport-agnostic relay logic for the nbclassic-mcp-bridge.

    Operates on duck-typed *peers*: any object with mutable ``role`` and
    ``notebook`` attributes (both ``None`` until a successful ``hello``) and
    ``send(text)`` / ``close(code, reason)`` methods. ``BridgeHandler`` is the
    production peer; tests use a list-backed fake. Keeping the routing here,
    free of any WebSocket type, is what makes it unit-testable without mocking.

    All methods are synchronous: ``send`` is fire-and-forget and ``close`` only
    schedules a close, so nothing here needs to await.
    """

    def __init__(self):
        # notebook path -> {role -> peer}. A room pairs the browser
        # ("extension") with the assistant ("mcp").
        self.rooms: dict[str, dict[str, object]] = {}

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
        room = self.rooms.setdefault(notebook, {})

        # Reconnect or a second browser tab: evict the stale peer in this role.
        existing = room.get(role)
        if existing is not None and existing is not peer:
            existing.close(1001, "replaced by a newer connection")

        room[role] = peer

        target = self._peer(peer)
        if target is not None:
            # Tell each side that the other is present.
            _send(target, {"kind": "status", "peer": role, "state": "joined"})
            _send(peer, {"kind": "status", "peer": _other(role), "state": "joined"})

    def _peer(self, peer):
        return self.rooms.get(peer.notebook, {}).get(_other(peer.role))
