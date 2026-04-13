"""File claim tests: agents declare intent to modify files,
broker detects conflicts, prevents overwrite races."""
import json
import time
from typing import Any

import pytest

from warroom.channel.broker import CLAIM_TTL_S, Broker, ConnState
from warroom.channel.db import init_db
from warroom.channel.protocol import FrameType


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        if self.closed:
            raise ConnectionError("closed")
        self.sent.append(json.loads(raw))

    async def close(self, *a: Any, **kw: Any) -> None:
        self.closed = True


@pytest.fixture
def broker():
    db = init_db(":memory:")
    b = Broker(db=db)
    yield b
    db.close()


async def _join(broker, actor, client_id="c1"):
    ws = FakeWebSocket()
    state = ConnState(ws=ws, client_id=client_id)
    await broker.handle_frame(state, {
        "op": "join", "req_id": "j1",
        "room": "room1", "actor": actor, "client_id": client_id,
    })
    return state, ws


# --- claim / release basics ---

async def test_claim_file_success(broker):
    state, ws = await _join(broker, "claude")
    await broker.handle_frame(state, {
        "op": "claim_file", "req_id": "c1",
        "room": "room1", "path": "auth.py",
    })
    ack = ws.sent[-1]
    assert ack["op"] == "file_claimed"
    assert ack["ok"] is True
    assert ack["path"] == "auth.py"


async def test_claim_conflict_detected(broker):
    state_a, ws_a = await _join(broker, "claude", "c1")
    state_b, ws_b = await _join(broker, "codex", "c2")

    # claude claims auth.py
    await broker.handle_frame(state_a, {
        "op": "claim_file", "req_id": "c1",
        "room": "room1", "path": "auth.py",
    })
    assert ws_a.sent[-1]["ok"] is True

    # codex tries to claim same file → conflict
    await broker.handle_frame(state_b, {
        "op": "claim_file", "req_id": "c2",
        "room": "room1", "path": "auth.py",
    })
    resp = ws_b.sent[-1]
    assert resp["op"] == "error"
    assert resp["code"] == "file_conflict"
    assert "claude" in resp["message"]


async def test_release_file_allows_reclaim(broker):
    state_a, ws_a = await _join(broker, "claude", "c1")
    state_b, ws_b = await _join(broker, "codex", "c2")

    # claude claims then releases
    await broker.handle_frame(state_a, {
        "op": "claim_file", "req_id": "c1",
        "room": "room1", "path": "auth.py",
    })
    await broker.handle_frame(state_a, {
        "op": "release_file", "req_id": "r1",
        "room": "room1", "path": "auth.py",
    })
    assert ws_a.sent[-1]["op"] == "file_released"

    # codex can now claim it
    await broker.handle_frame(state_b, {
        "op": "claim_file", "req_id": "c2",
        "room": "room1", "path": "auth.py",
    })
    assert ws_b.sent[-1]["ok"] is True


async def test_disconnect_releases_all_claims(broker):
    state, ws = await _join(broker, "claude")

    await broker.handle_frame(state, {
        "op": "claim_file", "req_id": "c1",
        "room": "room1", "path": "auth.py",
    })
    await broker.handle_frame(state, {
        "op": "claim_file", "req_id": "c2",
        "room": "room1", "path": "db.py",
    })

    await broker.on_disconnect(state)

    # Another agent can now claim both
    state_b, ws_b = await _join(broker, "codex", "c2")
    await broker.handle_frame(state_b, {
        "op": "claim_file", "req_id": "c3",
        "room": "room1", "path": "auth.py",
    })
    assert ws_b.sent[-1]["ok"] is True


async def test_claim_broadcasts_to_room(broker):
    state_a, ws_a = await _join(broker, "claude", "c1")
    state_b, ws_b = await _join(broker, "codex", "c2")

    await broker.handle_frame(state_a, {
        "op": "claim_file", "req_id": "c1",
        "room": "room1", "path": "auth.py",
    })

    # codex should receive a broadcast about claude's claim
    broadcasts = [f for f in ws_b.sent if f["op"] == "broadcast"]
    assert len(broadcasts) == 1
    assert broadcasts[0]["msg"]["content"] == "[system] claude claimed auth.py"


async def test_same_actor_reclaim_idempotent(broker):
    state, ws = await _join(broker, "claude")

    await broker.handle_frame(state, {
        "op": "claim_file", "req_id": "c1",
        "room": "room1", "path": "auth.py",
    })
    await broker.handle_frame(state, {
        "op": "claim_file", "req_id": "c2",
        "room": "room1", "path": "auth.py",
    })
    assert ws.sent[-1]["op"] == "file_claimed"
    assert ws.sent[-1]["ok"] is True


async def test_list_claims(broker):
    state, ws = await _join(broker, "claude")

    await broker.handle_frame(state, {
        "op": "claim_file", "req_id": "c1",
        "room": "room1", "path": "auth.py",
    })
    await broker.handle_frame(state, {
        "op": "claim_file", "req_id": "c2",
        "room": "room1", "path": "db.py",
    })
    await broker.handle_frame(state, {
        "op": "list_claims", "req_id": "l1",
        "room": "room1",
    })

    resp = ws.sent[-1]
    assert resp["op"] == "claims_list"
    assert resp["ok"] is True
    claims = resp["claims"]
    assert len(claims) == 2
    # Claims now include claimed_at timestamp; check path+actor
    claim_summaries = [{"path": c["path"], "actor": c["actor"]} for c in claims]
    assert {"path": "auth.py", "actor": "claude"} in claim_summaries
    assert {"path": "db.py", "actor": "claude"} in claim_summaries
    # Verify claimed_at is present
    assert all("claimed_at" in c for c in claims)


async def test_expire_stale_claims_auto_releases_and_broadcasts(broker):
    state_a, ws_a = await _join(broker, "claude", "c1")
    state_b, ws_b = await _join(broker, "codex", "c2")

    await broker.handle_frame(state_a, {
        "op": "claim_file", "req_id": "c1",
        "room": "room1", "path": "auth.py",
    })
    broker.file_claims[("room1", "auth.py")] = (
        "claude",
        time.time() - CLAIM_TTL_S - 1,
    )

    await broker.expire_stale_claims()

    assert ("room1", "auth.py") not in broker.file_claims
    broadcasts = [f for f in ws_b.sent if f["op"] == "broadcast"]
    assert any("claim expired" in f["msg"]["content"] for f in broadcasts)


async def test_reclaim_refreshes_claim_timestamp(broker):
    state, ws = await _join(broker, "claude")

    await broker.handle_frame(state, {
        "op": "claim_file", "req_id": "c1",
        "room": "room1", "path": "auth.py",
    })
    first_ts = broker.file_claims[("room1", "auth.py")][1]

    await broker.handle_frame(state, {
        "op": "claim_file", "req_id": "c2",
        "room": "room1", "path": "auth.py",
    })
    refreshed_ts = broker.file_claims[("room1", "auth.py")][1]

    assert ws.sent[-1]["op"] == "file_claimed"
    assert ws.sent[-1]["already_claimed"] is True
    assert refreshed_ts >= first_ts
