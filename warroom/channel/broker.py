"""Broker core: frame-level state machine for a channel room.

This module is transport-agnostic — it takes already-parsed dict frames
and an abstract `ConnState` that knows how to `send` bytes. The actual
WebSocket server plumbing lives in `broker_server.py`.

State:
  - rooms: {room_name: [ConnState, ...]}     subscribers per room
  - active_joins: {(room, actor): client_id} enforces one shim per (room, actor)

Frame handling:
  - JOIN  → check duplicate_actor, insert into rooms, send JOINED or ERROR
  - POST  → insert to db, broadcast to room (exclude poster), send POSTED
  - unknown → send ERROR{code=unknown_op}

On disconnect: remove from rooms, drop active_joins entry if owned.

Broadcast rule (fixes v1 HIGH 3): exclude by client_id, NOT by actor name.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from warroom.channel.db import insert_message
from warroom.channel.protocol import FrameType, Message


class WebSocketLike(Protocol):
    """The tiny surface broker needs from its transport. Satisfied by both
    real `websockets.ServerConnection` and the FakeWebSocket used in tests."""

    async def send(self, raw: str) -> None: ...
    async def close(self, *args: Any, **kwargs: Any) -> None: ...


@dataclass
class ConnState:
    """Per-connection state tracked by the broker."""

    ws: WebSocketLike
    client_id: str
    actor: str | None = None
    joined_rooms: set[str] = field(default_factory=set)


class Broker:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self.rooms: dict[str, list[ConnState]] = {}
        self.active_joins: dict[tuple[str, str], ConnState] = {}
        # File claims: {(room, path): actor} — lightweight lock to prevent
        # two agents from simultaneously editing the same file.
        self.file_claims: dict[tuple[str, str], str] = {}

    # --- top-level entry points ---

    async def handle_frame(self, state: ConnState, frame: dict[str, Any]) -> None:
        op = frame.get("op")
        if op == FrameType.JOIN:
            await self._on_join(state, frame)
        elif op == FrameType.POST:
            await self._on_post(state, frame)
        elif op == "claim_file":
            await self._on_claim_file(state, frame)
        elif op == "release_file":
            await self._on_release_file(state, frame)
        elif op == "list_claims":
            await self._on_list_claims(state, frame)
        elif op == FrameType.PING:
            await self._send(state, {
                "op": FrameType.PONG,
                "reply_to_req_id": frame.get("req_id"),
                "ok": True,
            })
        else:
            await self._send(state, {
                "op": FrameType.ERROR,
                "reply_to_req_id": frame.get("req_id"),
                "code": "unknown_op",
                "message": f"unknown op {op!r}",
            })

    async def on_disconnect(self, state: ConnState) -> None:
        """Clean up rooms + active_joins for a dying connection.

        v5 HIGH 1 fix: compare by ConnState IDENTITY, not client_id. A stale
        disconnect of an old ConnState must not clear a newer ConnState's
        claim, even if both happen to have the same client_id (idempotent
        rejoin scenario).
        """
        for room in list(state.joined_rooms):
            if room in self.rooms and state in self.rooms[room]:
                self.rooms[room].remove(state)
                if not self.rooms[room]:
                    del self.rooms[room]
            if state.actor is not None:
                key = (room, state.actor)
                if self.active_joins.get(key) is state:
                    del self.active_joins[key]
            # Release all file claims owned by this actor in this room
            if state.actor is not None:
                to_release = [
                    k for k, v in self.file_claims.items()
                    if k[0] == room and v == state.actor
                ]
                for k in to_release:
                    del self.file_claims[k]
        state.joined_rooms.clear()

    # --- handlers ---

    async def _on_join(self, state: ConnState, frame: dict[str, Any]) -> None:
        req_id = frame.get("req_id")
        room = frame.get("room")
        actor = frame.get("actor")
        client_id = frame.get("client_id")
        if not (isinstance(room, str) and isinstance(actor, str) and isinstance(client_id, str)):
            await self._send(state, {
                "op": FrameType.ERROR,
                "reply_to_req_id": req_id,
                "code": "bad_request",
                "message": "join requires string room, actor, client_id",
            })
            return

        key = (room, actor)
        existing = self.active_joins.get(key)
        # v5 HIGH 1 fix: accept idempotent re-join only if it's the SAME
        # ConnState. Cross-connection idempotence-by-client_id is too risky:
        # a stale disconnect of the old connection would then clear the
        # claim out from under the new one.
        if existing is not None and existing is not state:
            await self._send(state, {
                "op": FrameType.ERROR,
                "reply_to_req_id": req_id,
                "code": "duplicate_actor",
                "message": f"actor {actor!r} already joined {room!r}",
            })
            return

        # Accept: either fresh join or idempotent re-join by the SAME state.
        self.active_joins[key] = state
        state.actor = actor
        state.client_id = client_id
        state.joined_rooms.add(room)
        self.rooms.setdefault(room, [])
        if state not in self.rooms[room]:
            self.rooms[room].append(state)

        last_msg_id = self._last_msg_id(room)
        await self._send(state, {
            "op": FrameType.JOINED,
            "reply_to_req_id": req_id,
            "room": room,
            "last_msg_id": last_msg_id,
            "ok": True,
        })

    async def _on_post(self, state: ConnState, frame: dict[str, Any]) -> None:
        req_id = frame.get("req_id")
        room = frame.get("room")
        reply_to = frame.get("reply_to")
        client_id = state.client_id

        # A2A format: accept either `parts` array or legacy `content` string
        parts = frame.get("parts")
        content = frame.get("content")
        role = frame.get("role", "agent")

        if not isinstance(room, str):
            await self._send(state, {
                "op": FrameType.ERROR,
                "reply_to_req_id": req_id,
                "code": "bad_request",
                "message": "post requires string room",
            })
            return
        if parts is None and content is None:
            await self._send(state, {
                "op": FrameType.ERROR,
                "reply_to_req_id": req_id,
                "code": "bad_request",
                "message": "post requires parts array or content string",
            })
            return
        if room not in state.joined_rooms:
            await self._send(state, {
                "op": FrameType.ERROR,
                "reply_to_req_id": req_id,
                "code": "not_joined",
                "message": f"must join {room!r} before posting",
            })
            return

        # Build A2A-compatible parts
        from warroom.channel.protocol import text_part
        if parts is None:
            parts = [text_part(str(content))]

        actor = state.actor or "unknown"
        ts = time.time()
        msg = Message(
            id=0,
            ts=ts,
            room=room,
            actor=actor,
            client_id=client_id,
            role=str(role),
            parts=parts if isinstance(parts, list) else [],
            reply_to=reply_to if isinstance(reply_to, int) else None,
        )
        new_id = insert_message(self._db, msg)
        msg.id = new_id

        # Ack to poster
        await self._send(state, {
            "op": FrameType.POSTED,
            "reply_to_req_id": req_id,
            "room": room,
            "msg_id": new_id,
            "ts": ts,
            "ok": True,
        })

        # Broadcast to all room subscribers EXCEPT poster (by client_id)
        await self._broadcast(room, msg.to_dict(), exclude_client_id=client_id)

    # --- file claims ---

    async def _on_claim_file(self, state: ConnState, frame: dict[str, Any]) -> None:
        req_id = frame.get("req_id")
        room = frame.get("room")
        path = frame.get("path")
        actor = state.actor

        if not (isinstance(room, str) and isinstance(path, str)):
            await self._send(state, {
                "op": FrameType.ERROR, "reply_to_req_id": req_id,
                "code": "bad_request", "message": "claim_file requires room and path",
            })
            return

        # MED 1 fix: must be joined with a real actor name
        if actor is None or room not in state.joined_rooms:
            await self._send(state, {
                "op": FrameType.ERROR, "reply_to_req_id": req_id,
                "code": "not_joined", "message": "must join room before claiming files",
            })
            return

        key = (room, path)
        existing = self.file_claims.get(key)

        if existing is not None and existing != actor:
            await self._send(state, {
                "op": FrameType.ERROR, "reply_to_req_id": req_id,
                "code": "file_conflict",
                "message": f"{path} is already claimed by {existing}",
            })
            return

        # LOW 3 fix: idempotent re-claim = ack only, no broadcast/db insert
        if existing == actor:
            await self._send(state, {
                "op": "file_claimed", "reply_to_req_id": req_id,
                "ok": True, "path": path, "actor": actor, "already_claimed": True,
            })
            return

        # Fresh claim
        self.file_claims[key] = actor
        await self._send(state, {
            "op": "file_claimed", "reply_to_req_id": req_id,
            "ok": True, "path": path, "actor": actor,
        })

        # Broadcast system message so everyone sees the claim
        from warroom.channel.protocol import Message, text_part
        ts = time.time()
        msg = Message(
            id=0, ts=ts, room=room, actor=actor,
            client_id=state.client_id,
            parts=[text_part(f"[system] {actor} claimed {path}")],
        )
        new_id = insert_message(self._db, msg)
        msg.id = new_id
        await self._broadcast(room, msg.to_dict(), exclude_client_id=state.client_id)

    async def _on_release_file(self, state: ConnState, frame: dict[str, Any]) -> None:
        req_id = frame.get("req_id")
        room = frame.get("room")
        path = frame.get("path")
        actor = state.actor

        if actor is None or not isinstance(room, str) or room not in state.joined_rooms:
            await self._send(state, {
                "op": FrameType.ERROR, "reply_to_req_id": req_id,
                "code": "not_joined", "message": "must join room before releasing files",
            })
            return

        key = (room, path) if isinstance(path, str) else (None, None)
        existing = self.file_claims.get(key)  # type: ignore[arg-type]
        if existing == actor:
            del self.file_claims[key]  # type: ignore[arg-type]

        await self._send(state, {
            "op": "file_released", "reply_to_req_id": req_id,
            "ok": True, "path": path,
        })

    async def _on_list_claims(self, state: ConnState, frame: dict[str, Any]) -> None:
        req_id = frame.get("req_id")
        room = frame.get("room", "room1")
        claims = [
            {"path": k[1], "actor": v}
            for k, v in self.file_claims.items()
            if k[0] == room
        ]
        await self._send(state, {
            "op": "claims_list", "reply_to_req_id": req_id,
            "ok": True, "claims": claims,
        })

    # --- helpers ---

    async def _broadcast(
        self,
        room: str,
        msg_dict: dict[str, Any],
        exclude_client_id: str | None = None,
    ) -> None:
        if room not in self.rooms:
            return
        frame = {"op": FrameType.BROADCAST, "room": room, "msg": msg_dict}
        dead: list[ConnState] = []
        for conn in self.rooms[room]:
            if exclude_client_id is not None and conn.client_id == exclude_client_id:
                continue
            try:
                await self._send(conn, frame)
            except Exception:
                dead.append(conn)
        for conn in dead:
            await self.on_disconnect(conn)

    async def _send(self, state: ConnState, frame: dict[str, Any]) -> None:
        import json
        await state.ws.send(json.dumps(frame, separators=(",", ":")))

    def _last_msg_id(self, room: str) -> int:
        cur = self._db.execute(
            "SELECT COALESCE(MAX(id), 0) FROM messages WHERE room = ?",
            (room,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
