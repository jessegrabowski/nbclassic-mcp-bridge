import json

import pytest

from nbclassic_mcp_bridge.switchboard import PROTOCOL_VERSION, Switchboard


class FakePeer:
    def __init__(self):
        self.role: str | None = None
        self.notebook: str | None = None
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None

    def send(self, text):
        self.sent.append(json.loads(text))

    def close(self, code, reason):
        self.closed = (code, reason)


def hello(role="mcp", notebook="nb.ipynb", protocol=PROTOCOL_VERSION):
    return json.dumps({"kind": "hello", "protocol": protocol, "role": role, "notebook": notebook})


def join(sb, role, notebook="nb.ipynb"):
    peer = FakePeer()
    sb.route(peer, hello(role, notebook))
    return peer


def test_hello_registers_peer_in_its_room():
    sb = Switchboard()
    peer = join(sb, "mcp")
    assert (peer.role, peer.notebook) == ("mcp", "nb.ipynb")
    assert sb.rooms["nb.ipynb"]["mcp"] is peer
    assert peer.closed is None


@pytest.mark.parametrize(
    "frame",
    [
        json.dumps(
            {
                "kind": "hello",
                "protocol": PROTOCOL_VERSION + 1,
                "role": "mcp",
                "notebook": "nb.ipynb",
            }
        ),
        json.dumps(
            {
                "kind": "hello",
                "protocol": PROTOCOL_VERSION,
                "role": "kernel",
                "notebook": "nb.ipynb",
            }
        ),
        json.dumps({"kind": "hello", "protocol": PROTOCOL_VERSION, "role": "mcp", "notebook": 123}),
    ],
    ids=["bad-protocol", "unknown-role", "non-string-notebook"],
)
def test_malformed_hello_closes_without_registering(frame):
    sb = Switchboard()
    peer = FakePeer()
    sb.route(peer, frame)
    assert peer.closed[0] == 1002
    assert peer.role is None
    assert sb.rooms == {}


def test_duplicate_hello_closes_the_peer():
    sb = Switchboard()
    peer = join(sb, "mcp")
    sb.route(peer, hello("mcp"))
    assert peer.closed[0] == 1002


@pytest.mark.parametrize(
    "frame, expected_code",
    [
        (json.dumps({"kind": "cmd", "id": 1, "op": "snapshot", "args": {}}), 1002),
        ("{not json", 1003),
        ("123", 1003),
        ("null", 1003),
        ("[1, 2]", 1003),
        ('"hello"', 1003),
    ],
    ids=["frame-before-hello", "malformed-json", "json-int", "json-null", "json-list", "json-str"],
)
def test_route_rejects_bad_input(frame, expected_code):
    sb = Switchboard()
    peer = FakePeer()
    sb.route(peer, frame)
    assert peer.closed[0] == expected_code


@pytest.mark.parametrize(
    "sender_role, frame",
    [
        ("mcp", {"kind": "cmd", "id": 3, "op": "snapshot", "args": {}}),
        ("extension", {"kind": "reply", "id": 3, "ok": True, "result": []}),
        ("extension", {"kind": "event", "name": "cell_executed", "data": {"cell_id": "c1"}}),
    ],
    ids=["cmd", "reply", "event"],
)
def test_frame_is_forwarded_to_the_other_peer_only(sender_role, frame):
    sb = Switchboard()
    peers = {"extension": join(sb, "extension"), "mcp": join(sb, "mcp")}
    sender = peers[sender_role]
    other = peers["mcp" if sender_role == "extension" else "extension"]
    sb.route(sender, json.dumps(frame))
    assert other.sent[-1] == frame
    assert frame not in sender.sent


@pytest.mark.parametrize(
    "role, missing_role",
    [("mcp", "extension"), ("extension", "mcp")],
)
def test_cmd_without_a_peer_gets_an_error_reply(role, missing_role):
    sb = Switchboard()
    peer = join(sb, role)
    sb.route(peer, json.dumps({"kind": "cmd", "id": 7, "op": "snapshot", "args": {}}))
    assert peer.sent[-1] == {
        "kind": "reply",
        "id": 7,
        "ok": False,
        "error": f"no {missing_role} peer connected",
    }


def test_event_without_a_peer_is_dropped():
    sb = Switchboard()
    ext = join(sb, "extension")
    sb.route(ext, json.dumps({"kind": "event", "name": "cell_executed", "data": {}}))
    assert ext.sent == []
    assert ext.closed is None


def test_rooms_are_isolated_by_notebook():
    sb = Switchboard()
    ext_a = join(sb, "extension", "a.ipynb")
    mcp_b = join(sb, "mcp", "b.ipynb")
    sb.route(mcp_b, json.dumps({"kind": "cmd", "id": 1, "op": "snapshot", "args": {}}))
    assert ext_a.sent == []
    assert mcp_b.sent[-1]["ok"] is False


def test_join_notifies_both_peers():
    sb = Switchboard()
    ext = join(sb, "extension")
    assert ext.sent == []
    mcp = join(sb, "mcp")
    assert ext.sent[-1] == {"kind": "status", "peer": "mcp", "state": "joined"}
    assert mcp.sent[-1] == {"kind": "status", "peer": "extension", "state": "joined"}


def test_leave_notifies_the_surviving_peer():
    sb = Switchboard()
    ext = join(sb, "extension")
    mcp = join(sb, "mcp")
    ext.sent.clear()
    sb.leave(mcp)
    assert ext.sent[-1] == {"kind": "status", "peer": "mcp", "state": "left"}


def test_leave_removes_the_emptied_room():
    sb = Switchboard()
    mcp = join(sb, "mcp")
    sb.leave(mcp)
    assert sb.rooms == {}


def test_leave_before_hello_is_a_noop():
    sb = Switchboard()
    peer = FakePeer()
    sb.leave(peer)
    assert sb.rooms == {}
    assert peer.closed is None


def test_reconnecting_evicts_the_stale_peer():
    sb = Switchboard()
    first = join(sb, "mcp")
    second = join(sb, "mcp")
    assert first.closed == (1001, "replaced by a newer connection")
    assert sb.rooms["nb.ipynb"]["mcp"] is second


def test_evicted_peer_leaving_does_not_disturb_the_room():
    sb = Switchboard()
    ext = join(sb, "extension")
    first = join(sb, "mcp")
    second = join(sb, "mcp")
    ext.sent.clear()
    sb.leave(first)
    assert sb.rooms["nb.ipynb"]["mcp"] is second
    assert ext.sent == []
