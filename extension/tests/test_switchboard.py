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
    ],
    ids=["cmd", "reply"],
)
def test_frame_is_forwarded_to_the_other_peer_only(sender_role, frame):
    sb = Switchboard()
    peers = {"extension": join(sb, "extension"), "mcp": join(sb, "mcp")}
    sender = peers[sender_role]
    other = peers["mcp" if sender_role == "extension" else "extension"]
    sb.route(sender, json.dumps(frame))
    assert other.sent[-1] == frame
    assert frame not in sender.sent


def test_events_are_stamped_and_forwarded():
    sb = Switchboard()
    ext = join(sb, "extension")
    mcp = join(sb, "mcp")
    sb.route(ext, json.dumps({"kind": "event", "name": "cell_deleted", "data": {"cell_id": "c1"}}))
    sb.route(ext, json.dumps({"kind": "event", "name": "cell_created", "data": {"cell_id": "c2"}}))
    stamped = [frame for frame in mcp.sent if frame["kind"] == "event"]
    assert [frame["seq"] for frame in stamped] == [1, 2]
    assert all(frame["log_id"] == sb.log_id for frame in stamped)


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


def test_two_mcp_peers_in_distinct_rooms_coexist():
    # One assistant per notebook, but rooms are independent: a second mcp peer helloing into a
    # different notebook must not evict the first. Attaching to several notebooks at once rests
    # entirely on this, so it is pinned rather than left incidental.
    sb = Switchboard()
    ext_a = join(sb, "extension", "a.ipynb")
    ext_b = join(sb, "extension", "b.ipynb")
    mcp_a = join(sb, "mcp", "a.ipynb")
    mcp_b = join(sb, "mcp", "b.ipynb")

    assert (mcp_a.closed, mcp_b.closed) == (None, None)
    assert sb.rooms["a.ipynb"]["mcp"] is mcp_a
    assert sb.rooms["b.ipynb"]["mcp"] is mcp_b
    assert sb.presence() == {"a.ipynb": ["extension", "mcp"], "b.ipynb": ["extension", "mcp"]}

    sb.route(mcp_a, json.dumps({"kind": "cmd", "id": 1, "op": "snapshot", "args": {}}))
    sb.route(mcp_b, json.dumps({"kind": "cmd", "id": 2, "op": "snapshot", "args": {}}))
    assert [frame["id"] for frame in ext_a.sent if frame["kind"] == "cmd"] == [1]
    assert [frame["id"] for frame in ext_b.sent if frame["kind"] == "cmd"] == [2]


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


def test_presence_maps_live_rooms_to_roles():
    sb = Switchboard()
    join(sb, "extension", "a.ipynb")
    join(sb, "extension", "b.ipynb")
    join(sb, "mcp", "b.ipynb")
    assert sb.presence() == {"a.ipynb": ["extension"], "b.ipynb": ["extension", "mcp"]}


def test_presence_forgets_departed_rooms():
    sb = Switchboard()
    peer = join(sb, "mcp")
    sb.leave(peer)
    assert sb.presence() == {}


def _emit(sb, ext, name, cell_id):
    sb.route(ext, json.dumps({"kind": "event", "name": name, "data": {"cell_id": cell_id}}))


def test_events_and_their_numbering_are_scoped_to_one_room():
    # Each room numbers its own events from 1, and an event reaches only its own room's mcp peer.
    # A client's replay position is therefore meaningless outside the room it was learned in.
    sb = Switchboard()
    ext_a = join(sb, "extension", "a.ipynb")
    ext_b = join(sb, "extension", "b.ipynb")
    mcp_a = join(sb, "mcp", "a.ipynb")
    mcp_b = join(sb, "mcp", "b.ipynb")

    _emit(sb, ext_a, "cell_created", "c1")
    _emit(sb, ext_a, "cell_deleted", "c1")
    _emit(sb, ext_b, "cell_created", "c9")

    events_a = [frame for frame in mcp_a.sent if frame["kind"] == "event"]
    events_b = [frame for frame in mcp_b.sent if frame["kind"] == "event"]
    assert [frame["seq"] for frame in events_a] == [1, 2]
    assert [frame["seq"] for frame in events_b] == [1]
    assert [frame["data"]["cell_id"] for frame in events_b] == ["c9"]


def test_events_buffered_while_mcp_is_absent_replay_on_join():
    sb = Switchboard()
    ext = join(sb, "extension")
    _emit(sb, ext, "cell_created", "a")
    _emit(sb, ext, "source_changed", "a")

    mcp = join(sb, "mcp")
    replayed = [frame for frame in mcp.sent if frame["kind"] == "event"]
    assert [frame["name"] for frame in replayed] == ["cell_created", "source_changed"]
    # the join notification still precedes the replay
    assert mcp.sent[0] == {"kind": "status", "peer": "extension", "state": "joined"}


def test_replay_skips_events_the_peer_already_saw():
    sb = Switchboard()
    ext = join(sb, "extension")
    _emit(sb, ext, "cell_created", "a")
    _emit(sb, ext, "cell_deleted", "a")

    returning = FakePeer()
    sb.route(
        returning,
        json.dumps(
            {
                "kind": "hello",
                "protocol": PROTOCOL_VERSION,
                "role": "mcp",
                "notebook": "nb.ipynb",
                "last_event_seq": 1,
                "log_id": sb.log_id,
            }
        ),
    )
    replayed = [frame for frame in returning.sent if frame["kind"] == "event"]
    assert [frame["seq"] for frame in replayed] == [2]


def test_replay_ignores_a_position_from_another_log():
    sb = Switchboard()
    ext = join(sb, "extension")
    _emit(sb, ext, "cell_created", "a")

    returning = FakePeer()
    sb.route(
        returning,
        json.dumps(
            {
                "kind": "hello",
                "protocol": PROTOCOL_VERSION,
                "role": "mcp",
                "notebook": "nb.ipynb",
                "last_event_seq": 99,
                "log_id": "some-older-relay",
            }
        ),
    )
    replayed = [frame for frame in returning.sent if frame["kind"] == "event"]
    assert [frame["seq"] for frame in replayed] == [1]


def test_event_buffer_is_bounded(monkeypatch):
    import nbclassic_mcp_bridge.switchboard as switchboard_module

    monkeypatch.setattr(switchboard_module, "EVENT_BUFFER_MAXLEN", 3)
    sb = Switchboard()
    ext = join(sb, "extension")
    for n in range(5):
        _emit(sb, ext, "cell_created", f"c{n}")

    mcp = join(sb, "mcp")
    replayed = [frame for frame in mcp.sent if frame["kind"] == "event"]
    assert [frame["seq"] for frame in replayed] == [3, 4, 5]


def test_buffer_dies_with_the_room():
    sb = Switchboard()
    ext = join(sb, "extension")
    _emit(sb, ext, "cell_created", "a")
    sb.leave(ext)

    mcp = join(sb, "mcp")
    assert [frame for frame in mcp.sent if frame["kind"] == "event"] == []


def test_replay_coerces_a_malformed_position_to_full_replay():
    sb = Switchboard()
    ext = join(sb, "extension")
    _emit(sb, ext, "cell_created", "a")

    returning = FakePeer()
    sb.route(
        returning,
        json.dumps(
            {
                "kind": "hello",
                "protocol": PROTOCOL_VERSION,
                "role": "mcp",
                "notebook": "nb.ipynb",
                "last_event_seq": "not-a-number",
                "log_id": sb.log_id,
            }
        ),
    )
    replayed = [frame for frame in returning.sent if frame["kind"] == "event"]
    assert [frame["seq"] for frame in replayed] == [1]


def test_buffer_and_numbering_survive_extension_churn():
    # The room outlives a reconnecting browser tab as long as the mcp peer holds it, so the
    # buffer keeps accumulating and the numbering never restarts.
    sb = Switchboard()
    ext = join(sb, "extension")
    mcp = join(sb, "mcp")
    _emit(sb, ext, "cell_created", "a")
    sb.leave(ext)

    ext = join(sb, "extension")
    _emit(sb, ext, "cell_deleted", "a")
    seqs = [frame["seq"] for frame in mcp.sent if frame["kind"] == "event"]
    assert seqs == [1, 2]

    late = FakePeer()
    sb.route(
        late,
        json.dumps(
            {
                "kind": "hello",
                "protocol": PROTOCOL_VERSION,
                "role": "mcp",
                "notebook": "nb.ipynb",
                "last_event_seq": 0,
                "log_id": None,
            }
        ),
    )
    assert [frame["seq"] for frame in late.sent if frame["kind"] == "event"] == [1, 2]


def _hello_with_capabilities(sb, peer, capabilities):
    sb.route(
        peer,
        json.dumps(
            {
                "kind": "hello",
                "protocol": PROTOCOL_VERSION,
                "role": "extension",
                "notebook": "nb.ipynb",
                "capabilities": capabilities,
            }
        ),
    )


def test_extension_capabilities_reach_the_mcp_peer_in_both_join_orders():
    sb = Switchboard()
    ext = FakePeer()
    _hello_with_capabilities(sb, ext, ["snapshot", "inspect"])
    mcp = join(sb, "mcp")
    joined = next(frame for frame in mcp.sent if frame["kind"] == "status")
    assert joined["capabilities"] == ["snapshot", "inspect"]

    sb = Switchboard()
    mcp = join(sb, "mcp")
    ext = FakePeer()
    _hello_with_capabilities(sb, ext, ["snapshot"])
    joined = [frame for frame in mcp.sent if frame["kind"] == "status"][-1]
    assert joined == {"kind": "status", "peer": "extension", "state": "joined", "capabilities": ["snapshot"]}


def test_legacy_extension_hello_yields_a_capability_free_status():
    sb = Switchboard()
    join(sb, "extension")
    mcp = join(sb, "mcp")
    joined = next(frame for frame in mcp.sent if frame["kind"] == "status")
    assert joined == {"kind": "status", "peer": "extension", "state": "joined"}
