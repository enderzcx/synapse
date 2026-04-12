"""Step 5 tests: ChannelClient lifecycle + reader task + close race.

Uses the real broker_server as a fixture (Step 4 pattern reused)."""
import asyncio

import pytest

from warroom.channel.broker_server import serve
from warroom.channel.ws_client import ChannelClient


@pytest.fixture
async def broker_url():
    """v5 LOW 4 fix: use port=0 so the server picks a free port itself
    and we avoid the TOCTOU race of bind-close-reopen."""
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
    assert bound
    port = bound[0]
    try:
        yield f"ws://127.0.0.1:{port}"
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


# --- basic join / post / wait ---

async def test_join_and_post_basic(broker_url):
    c = ChannelClient(broker_url, actor="claude")
    await c.connect()
    try:
        resp = await c.join("room1")
        assert resp["op"] == "joined"
        assert resp["ok"] is True

        ack = await c.post("room1", content="hello")
        assert ack["op"] == "posted"
        assert ack["ok"] is True
        assert ack["msg_id"] >= 1
    finally:
        await c.close()


async def test_two_clients_broadcast_and_self_filter(broker_url):
    a = ChannelClient(broker_url, actor="claude")
    b = ChannelClient(broker_url, actor="codex")
    await a.connect()
    await b.connect()
    try:
        await a.join("room1")
        await b.join("room1")

        await a.post("room1", content="hi codex")

        # b receives broadcast
        msg = await asyncio.wait_for(b.wait_new("room1", timeout_s=3.0), timeout=4.0)
        assert msg is not None
        assert msg["content"] == "hi codex"
        assert msg["actor"] == "claude"
        assert msg["client_id"] == a.client_id

        # a does NOT receive its own post (self filter by client_id)
        self_msg = await a.wait_new("room1", timeout_s=0.5)
        assert self_msg is None
    finally:
        await a.close()
        await b.close()


async def test_duplicate_actor_rejected(broker_url):
    a = ChannelClient(broker_url, actor="claude")
    await a.connect()
    try:
        await a.join("room1")
        # Second client claiming same actor must get rejected
        b = ChannelClient(broker_url, actor="claude")
        await b.connect()
        try:
            with pytest.raises(ConnectionError) as exc:
                await b.join("room1")
            assert "duplicate_actor" in str(exc.value)
        finally:
            await b.close()
    finally:
        await a.close()


# --- close lifecycle ---

async def test_close_is_idempotent(broker_url):
    c = ChannelClient(broker_url, actor="claude")
    await c.connect()
    await c.close()
    await c.close()  # must not raise


async def test_request_after_close_raises(broker_url):
    c = ChannelClient(broker_url, actor="claude")
    await c.connect()
    await c.join("room1")
    await c.close()
    with pytest.raises(ConnectionError):
        await c.post("room1", content="too late")


async def test_wait_new_raises_when_client_closed_midflight(broker_url):
    c = ChannelClient(broker_url, actor="claude")
    await c.connect()
    await c.join("room1")

    async def _closer():
        await asyncio.sleep(0.2)
        await c.close()

    closer = asyncio.create_task(_closer())
    try:
        with pytest.raises(ConnectionError):
            await c.wait_new("room1", timeout_s=5.0)
    finally:
        await closer


async def test_broker_shutdown_fails_pending_requests(broker_url):
    """When the broker goes away mid-flight, any pending request must fail
    with ConnectionError (not hang until its own timeout)."""
    import websockets
    c = ChannelClient(broker_url, actor="claude")
    await c.connect()
    await c.join("room1")

    # Abruptly close the underlying ws from client side to simulate broker death
    assert c._ws is not None
    await c._ws.close()

    # Next post attempt should raise quickly (via reader finally drain or
    # send failure)
    with pytest.raises((ConnectionError, websockets.ConnectionClosed)):
        await c.post("room1", content="ghost")
    await c.close()


async def test_wait_new_timeout_returns_none(broker_url):
    c = ChannelClient(broker_url, actor="claude")
    await c.connect()
    try:
        await c.join("room1")
        msg = await c.wait_new("room1", timeout_s=0.3)
        assert msg is None
    finally:
        await c.close()
