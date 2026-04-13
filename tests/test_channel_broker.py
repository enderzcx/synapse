"""Step 3 RED: Broker core logic with a fake WebSocket.

These tests never touch a real socket — they verify the frame-handling
state machine: join / duplicate reject / post / broadcast / self-exclude /
multi-room / disconnect cleanup.
"""
import asyncio
import json
from typing import Any

import pytest

from warroom.channel.broker import Broker, ConnState
from warroom.channel.db import init_db
from warroom.channel.protocol import FrameType


class FakeWebSocket:
    """Minimal async WS stand-in. Tracks sent frames; closed on `close()`.

    v5 LOW 5 fix: once closed, send() raises — mimicking real websockets
    behavior — so broker dead-peer pruning in _broadcast() is exercised.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        if self.closed:
            raise ConnectionError("websocket is closed")
        self.sent.append(json.loads(raw))

    async def close(self, *args: Any, **kwargs: Any) -> None:
        self.closed = True


@pytest.fixture
def broker():
    db = init_db(":memory:")
    b = Broker(db=db)
    yield b
    db.close()


async def _join(broker: Broker, ws: FakeWebSocket, actor: str, room: str = "room1",
                client_id: str = "cid", req_id: str = "r1") -> ConnState:
    state = ConnState(ws=ws, client_id=client_id)
    await broker.handle_frame(state, {
        "op": FrameType.JOIN,
        "req_id": req_id,
        "room": room,
        "actor": actor,
        "client_id": client_id,
    })
    return state


# --- join ---

async def test_join_success(broker):
    ws = FakeWebSocket()
    state = await _join(broker, ws, actor="claude", client_id="c1")
    # Expect a `joined` ack
    assert len(ws.sent) == 1
    ack = ws.sent[0]
    assert ack["op"] == FrameType.JOINED
    assert ack["reply_to_req_id"] == "r1"
    assert ack["ok"] is True
    assert ack["room"] == "room1"
    # Broker state: v5 active_joins now holds ConnState (not client_id)
    assert ("room1", "claude") in broker.active_joins
    assert broker.active_joins[("room1", "claude")] is state
    assert state in broker.rooms["room1"]


async def test_join_duplicate_actor_rejected(broker):
    ws1 = FakeWebSocket()
    state1 = await _join(broker, ws1, actor="claude", client_id="c1", req_id="r1")
    # Second join with same actor but different client_id — must reject
    ws2 = FakeWebSocket()
    state2 = ConnState(ws=ws2, client_id="c2")
    await broker.handle_frame(state2, {
        "op": FrameType.JOIN,
        "req_id": "r2",
        "room": "room1",
        "actor": "claude",
        "client_id": "c2",
    })
    assert len(ws2.sent) == 1
    err = ws2.sent[0]
    assert err["op"] == FrameType.ERROR
    assert err["code"] == "duplicate_actor"
    assert err["reply_to_req_id"] == "r2"
    # Original claim still owns it (v5: identity-based)
    assert broker.active_joins[("room1", "claude")] is state1


async def test_join_same_state_idempotent(broker):
    """v5: idempotent re-join is now keyed on ConnState IDENTITY, not
    client_id. The same ConnState joining the same (room, actor) twice
    must still succeed."""
    ws = FakeWebSocket()
    state = await _join(broker, ws, actor="claude", client_id="c1", req_id="r1")
    # SAME state rejoining — idempotent success
    await broker.handle_frame(state, {
        "op": FrameType.JOIN,
        "req_id": "r2",
        "room": "room1",
        "actor": "claude",
        "client_id": "c1",
    })
    assert ws.sent[-1]["op"] == FrameType.JOINED
    assert ws.sent[-1]["ok"] is True
    assert broker.active_joins[("room1", "claude")] is state


# --- post + broadcast ---

async def test_post_acks_and_broadcasts(broker):
    ws_a = FakeWebSocket()
    await _join(broker, ws_a, actor="claude", client_id="c1", req_id="r1")
    ws_b = FakeWebSocket()
    await _join(broker, ws_b, actor="codex", client_id="c2", req_id="r2")

    # clientA (claude) posts
    state_a = broker.rooms["room1"][0]
    await broker.handle_frame(state_a, {
        "op": FrameType.POST,
        "req_id": "rp1",
        "room": "room1",
        "content": "hello codex",
        "client_id": "c1",
    })

    # clientA receives a `posted` ack
    ack = ws_a.sent[-1]
    assert ack["op"] == FrameType.POSTED
    assert ack["reply_to_req_id"] == "rp1"
    assert ack["ok"] is True
    assert ack["msg_id"] >= 1

    # clientB receives a `broadcast`
    bcast = ws_b.sent[-1]
    assert bcast["op"] == FrameType.BROADCAST
    assert "reply_to_req_id" not in bcast  # unsolicited
    assert bcast["msg"]["content"] == "hello codex"
    assert bcast["msg"]["actor"] == "claude"
    assert bcast["msg"]["client_id"] == "c1"


async def test_post_excludes_self_from_broadcast(broker):
    ws = FakeWebSocket()
    await _join(broker, ws, actor="claude", client_id="c1", req_id="r1")
    state = broker.rooms["room1"][0]
    initial_count = len(ws.sent)

    await broker.handle_frame(state, {
        "op": FrameType.POST,
        "req_id": "rp1",
        "room": "room1",
        "content": "echo?",
        "client_id": "c1",
    })

    # Only one new frame: the `posted` ack. No self-broadcast.
    posted_frames = [f for f in ws.sent[initial_count:] if f["op"] == FrameType.POSTED]
    broadcast_frames = [f for f in ws.sent[initial_count:] if f["op"] == FrameType.BROADCAST]
    assert len(posted_frames) == 1
    assert len(broadcast_frames) == 0


async def test_join_includes_recent_messages(broker):
    ws_a = FakeWebSocket()
    state_a = await _join(broker, ws_a, actor="claude", client_id="c1", req_id="j1")

    await broker.handle_frame(state_a, {
        "op": FrameType.POST,
        "req_id": "p1",
        "room": "room1",
        "content": "first message",
    })

    ws_b = FakeWebSocket()
    await _join(broker, ws_b, actor="codex", client_id="c2", req_id="j2")

    joined = ws_b.sent[0]
    assert joined["op"] == FrameType.JOINED
    assert joined["ok"] is True
    assert "recent_messages" in joined
    assert len(joined["recent_messages"]) == 1
    assert joined["recent_messages"][0]["content"] == "first message"


async def test_history_returns_messages_since_id(broker):
    ws_a = FakeWebSocket()
    state_a = await _join(broker, ws_a, actor="claude", client_id="c1", req_id="j1")

    await broker.handle_frame(state_a, {
        "op": FrameType.POST,
        "req_id": "p1",
        "room": "room1",
        "content": "one",
    })
    first_msg_id = ws_a.sent[-1]["msg_id"]

    await broker.handle_frame(state_a, {
        "op": FrameType.POST,
        "req_id": "p2",
        "room": "room1",
        "content": "two",
    })

    await broker.handle_frame(state_a, {
        "op": "history",
        "req_id": "h1",
        "room": "room1",
        "since_id": first_msg_id,
        "limit": 10,
    })

    resp = ws_a.sent[-1]
    assert resp["op"] == "history"
    assert resp["reply_to_req_id"] == "h1"
    assert resp["ok"] is True
    assert [m["content"] for m in resp["messages"]] == ["two"]


async def test_room_state_includes_active_agents_claims_and_last_msg_id(broker):
    ws_a = FakeWebSocket()
    state_a = await _join(broker, ws_a, actor="claude", client_id="c1", req_id="j1")
    ws_b = FakeWebSocket()
    await _join(broker, ws_b, actor="codex", client_id="c2", req_id="j2")

    await broker.handle_frame(state_a, {
        "op": "claim_file",
        "req_id": "c1",
        "room": "room1",
        "path": "auth.py",
    })
    await broker.handle_frame(state_a, {
        "op": FrameType.POST,
        "req_id": "p1",
        "room": "room1",
        "content": "hello",
    })
    last_msg_id = ws_a.sent[-1]["msg_id"]

    await broker.handle_frame(state_a, {
        "op": "room_state",
        "req_id": "s1",
        "room": "room1",
    })

    resp = ws_a.sent[-1]
    assert resp["op"] == "room_state"
    assert resp["reply_to_req_id"] == "s1"
    assert resp["ok"] is True
    assert {"actor": "claude", "client_id": "c1"} in resp["active_agents"]
    assert {"actor": "codex", "client_id": "c2"} in resp["active_agents"]
    assert any(c["path"] == "auth.py" and c["actor"] == "claude" for c in resp["claims"])
    assert resp["last_msg_id"] == last_msg_id


async def test_multi_room_isolation(broker):
    ws_a = FakeWebSocket()
    await _join(broker, ws_a, actor="claude", client_id="c1", room="room1", req_id="j1")
    ws_b = FakeWebSocket()
    await _join(broker, ws_b, actor="codex", client_id="c2", room="room2", req_id="j2")

    state_a = broker.rooms["room1"][0]
    await broker.handle_frame(state_a, {
        "op": FrameType.POST,
        "req_id": "rp1",
        "room": "room1",
        "content": "only in room1",
        "client_id": "c1",
    })
    # ws_b is only in room2, must not receive broadcast
    broadcasts = [f for f in ws_b.sent if f["op"] == FrameType.BROADCAST]
    assert broadcasts == []


# --- disconnect cleanup ---

async def test_disconnect_frees_active_join(broker):
    ws = FakeWebSocket()
    state = await _join(broker, ws, actor="claude", client_id="c1", req_id="r1")
    assert ("room1", "claude") in broker.active_joins

    await broker.on_disconnect(state)

    assert ("room1", "claude") not in broker.active_joins
    assert state not in broker.rooms.get("room1", [])


async def test_disconnect_allows_rejoin(broker):
    ws1 = FakeWebSocket()
    state1 = await _join(broker, ws1, actor="claude", client_id="c1", req_id="r1")
    await broker.on_disconnect(state1)

    ws2 = FakeWebSocket()
    state2 = await _join(broker, ws2, actor="claude", client_id="c2", req_id="r2")
    # Second join with different client_id must now succeed
    assert ws2.sent[-1]["op"] == FrameType.JOINED
    assert ws2.sent[-1]["ok"] is True
    assert broker.active_joins[("room1", "claude")] is state2


# --- error paths ---

async def test_unknown_op_returns_error(broker):
    ws = FakeWebSocket()
    state = ConnState(ws=ws, client_id="c1")
    await broker.handle_frame(state, {"op": "garbage", "req_id": "rx"})
    err = ws.sent[-1]
    assert err["op"] == FrameType.ERROR
    assert err["code"] == "unknown_op"
    assert err["reply_to_req_id"] == "rx"


# --- v5 regression tests for codex review round 4 findings ---

async def test_stale_disconnect_does_not_clear_newer_owner(broker):
    """v5 HIGH 1 regression: an old ConnState going through on_disconnect
    must NOT clear the (room, actor) claim if a newer ConnState now owns it.
    Without the fix, active_joins keyed on client_id alone would be wiped
    by the old connection's cleanup, letting a third client steal the actor
    while the owner is still live.
    """
    ws_old = FakeWebSocket()
    state_old = ConnState(ws=ws_old, client_id="shared-cid")
    await broker.handle_frame(state_old, {
        "op": FrameType.JOIN,
        "req_id": "j1",
        "room": "room1",
        "actor": "claude",
        "client_id": "shared-cid",
    })
    assert ws_old.sent[-1]["op"] == FrameType.JOINED
    assert broker.active_joins[("room1", "claude")] is state_old

    # A brand new ConnState happens to arrive with the SAME client_id.
    # Per v5 fix, this is NOT an idempotent rejoin (identity differs) —
    # it must be rejected so we don't have two ConnStates fighting over
    # the same actor claim.
    ws_new = FakeWebSocket()
    state_new = ConnState(ws=ws_new, client_id="shared-cid")
    await broker.handle_frame(state_new, {
        "op": FrameType.JOIN,
        "req_id": "j2",
        "room": "room1",
        "actor": "claude",
        "client_id": "shared-cid",
    })
    assert ws_new.sent[-1]["op"] == FrameType.ERROR
    assert ws_new.sent[-1]["code"] == "duplicate_actor"
    assert broker.active_joins[("room1", "claude")] is state_old


async def test_disconnect_only_clears_own_claim(broker):
    """If the old ConnState finally disconnects, active_joins[(room,actor)]
    should only be cleared when state_old is in fact the recorded owner.
    After the fix, this is identity-based comparison, so a stale disconnect
    of a never-actually-active ConnState must not clear anything.
    """
    ws_owner = FakeWebSocket()
    state_owner = await _join(broker, ws_owner, actor="claude",
                              client_id="c1", req_id="j1")
    assert broker.active_joins[("room1", "claude")] is state_owner

    # Construct a stale ConnState that shares the same client_id but
    # was never actually the owner (simulates race scenarios).
    stale_ws = FakeWebSocket()
    stale_state = ConnState(ws=stale_ws, client_id="c1")
    stale_state.actor = "claude"
    stale_state.joined_rooms.add("room1")
    await broker.on_disconnect(stale_state)

    # Owner's claim must survive
    assert broker.active_joins[("room1", "claude")] is state_owner


async def test_post_ignores_spoofed_client_id(broker):
    """v5 HIGH 2 regression: if a joined client sends a POST frame with
    a forged client_id, the broker must persist and broadcast the REAL
    client_id (from ConnState). Otherwise malicious clients could
    misattribute messages and suppress delivery via wait_new self-filter.
    """
    ws_a = FakeWebSocket()
    state_a = await _join(broker, ws_a, actor="claude",
                          client_id="real-cid-a", req_id="j1")
    ws_b = FakeWebSocket()
    state_b = await _join(broker, ws_b, actor="codex",
                          client_id="real-cid-b", req_id="j2")

    # claude (state_a) posts, forging codex's client_id in the frame
    await broker.handle_frame(state_a, {
        "op": FrameType.POST,
        "req_id": "p1",
        "room": "room1",
        "content": "spoofed",
        "client_id": "real-cid-b",   # <- forgery attempt
    })

    # codex (state_b) MUST still receive this broadcast because the broker
    # should ignore the forged client_id and use state_a's real one for
    # exclusion.
    broadcasts = [f for f in ws_b.sent if f["op"] == FrameType.BROADCAST]
    assert len(broadcasts) == 1
    msg = broadcasts[0]["msg"]
    assert msg["actor"] == "claude"
    assert msg["client_id"] == "real-cid-a"  # real, not forged

    # claude (state_a) must NOT receive its own broadcast regardless of forgery
    a_broadcasts = [f for f in ws_a.sent if f["op"] == FrameType.BROADCAST]
    assert a_broadcasts == []


async def test_broadcast_prunes_dead_peers(broker):
    """v5 LOW 5 regression: when a peer's ws has been closed (send raises),
    _broadcast must detect that and call on_disconnect so the peer doesn't
    sit in rooms / active_joins forever.
    """
    ws_poster = FakeWebSocket()
    state_poster = await _join(broker, ws_poster, actor="claude",
                               client_id="c1", req_id="j1")
    ws_dead = FakeWebSocket()
    state_dead = await _join(broker, ws_dead, actor="codex",
                             client_id="c2", req_id="j2")

    # Kill the dead peer's socket — subsequent broker sends to it will raise.
    await ws_dead.close()

    await broker.handle_frame(state_poster, {
        "op": FrameType.POST,
        "req_id": "p1",
        "room": "room1",
        "content": "hi",
    })

    # The dead peer should have been pruned out of rooms + active_joins
    assert state_dead not in broker.rooms.get("room1", [])
    assert ("room1", "codex") not in broker.active_joins
