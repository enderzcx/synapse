"""Step 4 e2e: spawn real broker_server, connect two websockets clients,
verify join/post/broadcast round trip against real network."""
import asyncio
import json
import uuid

import pytest
import websockets

from warroom.channel.broker_server import serve


@pytest.fixture
async def broker_server():
    """v5 LOW 4 fix: use port=0 so the server itself picks the free port.
    Eliminates the bind-close-reopen TOCTOU race that older tests had.
    """
    stop = asyncio.Event()
    ready = asyncio.Event()
    bound: list[int] = []
    task = asyncio.create_task(serve(
        host="127.0.0.1",
        port=0,
        db_path=":memory:",
        stop_event=stop,
        ready_event=ready,
        bound_port_box=bound,
    ))
    try:
        await asyncio.wait_for(ready.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        stop.set()
        await task
        raise RuntimeError("broker did not start")
    assert bound, "broker did not report its bound port"
    port = bound[0]
    try:
        yield port
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


async def _send(ws, obj):
    await ws.send(json.dumps(obj))


async def _recv(ws):
    return json.loads(await ws.recv())


async def test_real_broker_join_post_broadcast(broker_server):
    port = broker_server
    url = f"ws://127.0.0.1:{port}"

    async with websockets.connect(url) as ws_claude, \
               websockets.connect(url) as ws_codex:
        # Join as claude
        cid_claude = "cid-" + uuid.uuid4().hex[:8]
        await _send(ws_claude, {
            "op": "join", "req_id": "j1",
            "room": "room1", "actor": "claude", "client_id": cid_claude,
        })
        ack = await _recv(ws_claude)
        assert ack["op"] == "joined"
        assert ack["reply_to_req_id"] == "j1"
        assert ack["ok"] is True

        # Join as codex
        cid_codex = "cid-" + uuid.uuid4().hex[:8]
        await _send(ws_codex, {
            "op": "join", "req_id": "j2",
            "room": "room1", "actor": "codex", "client_id": cid_codex,
        })
        ack2 = await _recv(ws_codex)
        assert ack2["op"] == "joined"

        # claude posts
        await _send(ws_claude, {
            "op": "post", "req_id": "p1",
            "room": "room1", "content": "hello codex", "client_id": cid_claude,
        })
        # Next frames on claude: `posted` ack
        claude_ack = await _recv(ws_claude)
        assert claude_ack["op"] == "posted"
        assert claude_ack["reply_to_req_id"] == "p1"

        # codex receives broadcast (unsolicited)
        bcast = await _recv(ws_codex)
        assert bcast["op"] == "broadcast"
        assert "reply_to_req_id" not in bcast
        assert bcast["msg"]["content"] == "hello codex"
        assert bcast["msg"]["actor"] == "claude"
        assert bcast["msg"]["client_id"] == cid_claude


async def test_real_broker_duplicate_actor_rejected(broker_server):
    port = broker_server
    url = f"ws://127.0.0.1:{port}"
    async with websockets.connect(url) as ws_a, \
               websockets.connect(url) as ws_b:
        await _send(ws_a, {
            "op": "join", "req_id": "j1",
            "room": "room1", "actor": "claude", "client_id": "c1",
        })
        await _recv(ws_a)  # discard joined ack

        await _send(ws_b, {
            "op": "join", "req_id": "j2",
            "room": "room1", "actor": "claude", "client_id": "c2",
        })
        err = await _recv(ws_b)
        assert err["op"] == "error"
        assert err["code"] == "duplicate_actor"


async def test_real_broker_self_excluded_from_broadcast(broker_server):
    port = broker_server
    url = f"ws://127.0.0.1:{port}"
    async with websockets.connect(url) as ws:
        await _send(ws, {
            "op": "join", "req_id": "j1",
            "room": "room1", "actor": "claude", "client_id": "c1",
        })
        await _recv(ws)  # joined

        await _send(ws, {
            "op": "post", "req_id": "p1",
            "room": "room1", "content": "echo test", "client_id": "c1",
        })
        posted = await _recv(ws)
        assert posted["op"] == "posted"

        # Drain any pending frames with a short timeout — there should be
        # no broadcast for ourselves.
        try:
            extra = await asyncio.wait_for(ws.recv(), timeout=0.3)
            extra_frame = json.loads(extra)
            # If somehow something arrives, it must NOT be a broadcast of our own msg
            assert extra_frame["op"] != "broadcast", f"self broadcast leaked: {extra_frame}"
        except asyncio.TimeoutError:
            pass  # expected: no further frames


async def test_real_broker_join_returns_recent_messages(broker_server):
    port = broker_server
    url = f"ws://127.0.0.1:{port}"
    async with websockets.connect(url) as ws_a, websockets.connect(url) as ws_b:
        await _send(ws_a, {
            "op": "join", "req_id": "j1",
            "room": "room1", "actor": "claude", "client_id": "c1",
        })
        await _recv(ws_a)

        await _send(ws_a, {
            "op": "post", "req_id": "p1",
            "room": "room1", "content": "hello history", "client_id": "c1",
        })
        await _recv(ws_a)

        await _send(ws_b, {
            "op": "join", "req_id": "j2",
            "room": "room1", "actor": "codex", "client_id": "c2",
        })
        joined = await _recv(ws_b)
        assert joined["op"] == "joined"
        assert joined["ok"] is True
        assert [m["content"] for m in joined["recent_messages"]] == ["hello history"]
