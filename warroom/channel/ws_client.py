"""ChannelClient: WebSocket client with single reader task + demux.

Critical design (see docs/phase2-channel-design.md v4):

  * websockets library FORBIDS concurrent recv() on the same connection.
    We therefore spawn EXACTLY ONE background reader task per client.
  * All outgoing requests go through _request(), which registers a Future
    in self._pending keyed by req_id. The reader sets the future when the
    matching response frame arrives.
  * Broadcast frames have no reply_to_req_id; they are pushed to
    self._broadcasts (asyncio.Queue). wait_new() consumes them.

Lifecycle contract:
  * _request() is gated on _closed BEFORE and AFTER creating the future.
    Requests started after close() raise ConnectionError immediately.
  * _reader() drains _pending in a finally block on ANY exit path
    (normal close, websockets.ConnectionClosed, unexpected exception).
    It also puts _CLOSED_SENTINEL into _broadcasts to wake wait_new consumers.
  * close() is idempotent; closes ws with bounded timeout (transport.abort
    as fallback), then awaits reader exit with its own bounded timeout.
  * wait_new() raises ConnectionError on sentinel OR entry check.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

import websockets

from warroom.channel.protocol import FrameType

logger = logging.getLogger("a2a.channel.client")


# Unique sentinel placed in _broadcasts when reader exits, so wait_new
# consumers can distinguish "nothing new" from "client is gone".
_CLOSED_SENTINEL: dict[str, Any] = {"__closed__": True}


class ChannelClient:
    """One connection to a channel broker.

    Usage:
        c = ChannelClient("ws://127.0.0.1:9100", actor="claude")
        await c.connect()
        await c.join("room1")
        await c.post("room1", content="hello")
        msg = await c.wait_new("room1", timeout_s=60.0)
        await c.close()
    """

    def __init__(self, broker_url: str, actor: str) -> None:
        self.broker_url = broker_url
        self.actor = actor
        self.client_id = uuid.uuid4().hex
        self._ws: Any = None  # websockets connection, typed Any to avoid import at type level
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._broadcasts: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._closed = asyncio.Event()

    # --- lifecycle ---

    async def connect(self) -> None:
        if self._ws is not None:
            return
        self._ws = await websockets.connect(self.broker_url)
        self._reader_task = asyncio.create_task(self._reader())

    async def close(self) -> None:
        """Graceful shutdown. Idempotent. Bounded on all wait paths."""
        if self._closed.is_set():
            return
        self._closed.set()
        # 1) Close ws with a bounded timeout; transport.abort if handshake hangs
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                try:
                    transport = getattr(self._ws, "transport", None)
                    if transport is not None:
                        transport.abort()
                except Exception:
                    pass
        # 2) Await reader so we don't leak the task
        if self._reader_task is not None:
            try:
                await asyncio.wait_for(self._reader_task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._reader_task.cancel()
                try:
                    await self._reader_task
                except (asyncio.CancelledError, Exception):
                    pass

    # --- reader task (single consumer of ws.recv) ---

    async def _reader(self) -> None:
        """Demux incoming frames. Runs until ws closes or the task is cancelled.

        Exit path drains pending futures with ConnectionError and pushes
        _CLOSED_SENTINEL into broadcasts so wait_new consumers wake up.
        """
        try:
            assert self._ws is not None
            async for raw in self._ws:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("client got invalid json: %r", raw)
                    continue
                if not isinstance(frame, dict):
                    continue
                reply_to = frame.get("reply_to_req_id")
                if reply_to is not None:
                    fut = self._pending.pop(reply_to, None)
                    if fut is not None and not fut.done():
                        fut.set_result(frame)
                    continue
                if frame.get("op") == FrameType.BROADCAST:
                    # Unsolicited broadcast — drop into queue
                    await self._broadcasts.put(frame.get("msg") or {})
                    continue
                # Unknown unsolicited frame — log and ignore
                logger.debug("client got unknown unsolicited frame: %r", frame)
        except websockets.ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("reader exited on error: %r", e)
        finally:
            # v4 HIGH 2: drain pending on ANY exit path
            self._closed.set()
            pending_snapshot = self._pending
            self._pending = {}
            err = ConnectionError("channel client reader exited")
            for fut in pending_snapshot.values():
                if not fut.done():
                    fut.set_exception(err)
            # v4 HIGH 3: wake wait_new consumers
            try:
                self._broadcasts.put_nowait(_CLOSED_SENTINEL)
            except asyncio.QueueFull:
                pass

    # --- request/response ---

    async def _request(
        self,
        op: str,
        timeout: float = 10.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a request frame and await the matching response.

        Raises:
            ConnectionError: if the client is closed before or after
                             registering the future, or if the reader exits
                             with the future still pending.
        """
        # v4 HIGH 1: gate on _closed first
        if self._closed.is_set():
            raise ConnectionError("channel client is closed")
        assert self._ws is not None

        req_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        # v4 HIGH 1: second check after creating future — reader may have
        # drained _pending between the first check and now. Fail fast.
        if self._closed.is_set():
            raise ConnectionError("channel client is closed")
        self._pending[req_id] = fut

        frame = {"op": op, "req_id": req_id, **kwargs}
        raw = json.dumps(frame, separators=(",", ":"))
        try:
            await self._ws.send(raw)
        except Exception:
            self._pending.pop(req_id, None)
            raise

        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

        if resp.get("op") == FrameType.ERROR:
            code = resp.get("code", "unknown")
            message = resp.get("message", "")
            raise ConnectionError(f"broker error {code}: {message}")
        return resp

    async def join(self, room: str) -> dict[str, Any]:
        return await self._request(
            FrameType.JOIN,
            room=room,
            actor=self.actor,
            client_id=self.client_id,
        )

    async def post(
        self,
        room: str,
        content: str,
        reply_to: int | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            FrameType.POST,
            room=room,
            content=content,
            reply_to=reply_to,
            client_id=self.client_id,
        )

    async def ping(self) -> dict[str, Any]:
        return await self._request(FrameType.PING)

    # --- broadcast consumer ---

    def peek_new(self, room: str) -> list[dict[str, Any]]:
        """Non-blocking drain of buffered broadcast messages.

        Returns all currently queued messages for the given room (excluding
        self-sent). Does NOT block or contact the broker. Messages returned
        here will NOT appear in subsequent wait_new() calls.

        Safe to call during long tasks as a lightweight checkpoint.
        """
        messages: list[dict[str, Any]] = []
        requeue: list[dict[str, Any]] = []
        while True:
            try:
                msg = self._broadcasts.get_nowait()
            except asyncio.QueueEmpty:
                break
            if msg is _CLOSED_SENTINEL:
                # Put sentinel back so wait_new still sees it
                requeue.append(msg)
                break
            if not isinstance(msg, dict):
                continue
            if msg.get("client_id") == self.client_id:
                continue
            msg_room = msg.get("room")
            if msg_room is not None and msg_room != room:
                requeue.append(msg)
                continue
            messages.append(msg)
        # Put back messages that don't belong to this room / sentinel
        for item in requeue:
            try:
                self._broadcasts.put_nowait(item)
            except asyncio.QueueFull:
                pass
        return messages

    async def wait_new(self, room: str, timeout_s: float) -> dict[str, Any] | None:
        """Block until a broadcast arrives from a different client_id, or timeout.

        Returns the message dict on success, None on timeout.
        Raises ConnectionError if the client is (or becomes) closed.
        """
        if self._closed.is_set():
            raise ConnectionError("channel client is closed")
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                msg = await asyncio.wait_for(
                    self._broadcasts.get(), timeout=remaining
                )
            except asyncio.TimeoutError:
                return None
            # v4 HIGH 3: sentinel = reader exited → raise
            if msg is _CLOSED_SENTINEL:
                raise ConnectionError("channel client closed while waiting")
            if not isinstance(msg, dict):
                continue
            # Filter self by client_id (v4 HIGH 3 — NOT by actor)
            if msg.get("client_id") == self.client_id:
                continue
            # Filter non-target room
            msg_room = msg.get("room")
            if msg_room is not None and msg_room != room:
                continue
            return msg
