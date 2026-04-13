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

from warroom.channel.db import fetch_history, fetch_since, insert_message
from warroom.channel.protocol import FrameType, Message

# Claim TTL: auto-release claims older than this (seconds)
CLAIM_TTL_S = 600  # 10 minutes


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
        # File claims: {(room, path): (actor, claimed_at)} — lightweight lock
        # to prevent two agents from simultaneously editing the same file.
        self.file_claims: dict[tuple[str, str], tuple[str, float]] = {}

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
        elif op == "history":
            await self._on_history(state, frame)
        elif op == "room_state":
            await self._on_room_state(state, frame)
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
                    if k[0] == room and v[0] == state.actor
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
        is_reconnect = False

        if existing is not None and existing is not state:
            # Session restore: same actor reconnecting from a new connection.
            # Evict the old connection and let the new one take over.
            # This preserves file claims owned by the actor.
            if room in self.rooms and existing in self.rooms[room]:
                self.rooms[room].remove(existing)
            existing.joined_rooms.discard(room)
            is_reconnect = True

        # Accept: fresh join, idempotent re-join, or session restore.
        self.active_joins[key] = state
        state.actor = actor
        state.client_id = client_id
        state.joined_rooms.add(room)
        self.rooms.setdefault(room, [])
        if state not in self.rooms[room]:
            self.rooms[room].append(state)

        last_msg_id = self._last_msg_id(room)
        # History replay: send recent messages on join
        recent = fetch_history(self._db, room, limit=50)
        recent_dicts = [m.to_dict() for m in recent]
        await self._send(state, {
            "op": FrameType.JOINED,
            "reply_to_req_id": req_id,
            "room": room,
            "last_msg_id": last_msg_id,
            "recent_messages": recent_dicts,
            "is_reconnect": is_reconnect,
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
        claim = self.file_claims.get(key)
        existing_actor = claim[0] if claim else None

        if existing_actor is not None and existing_actor != actor:
            await self._send(state, {
                "op": FrameType.ERROR, "reply_to_req_id": req_id,
                "code": "file_conflict",
                "message": f"{path} is already claimed by {existing_actor}",
            })
            return

        # LOW 3 fix: idempotent re-claim = refresh timestamp + ack only
        if existing_actor == actor:
            self.file_claims[key] = (actor, time.time())  # refresh TTL
            await self._send(state, {
                "op": "file_claimed", "reply_to_req_id": req_id,
                "ok": True, "path": path, "actor": actor, "already_claimed": True,
            })
            return

        # Fresh claim
        self.file_claims[key] = (actor, time.time())
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
        claim = self.file_claims.get(key)  # type: ignore[arg-type]
        if claim is not None and claim[0] == actor:
            del self.file_claims[key]  # type: ignore[arg-type]

        await self._send(state, {
            "op": "file_released", "reply_to_req_id": req_id,
            "ok": True, "path": path,
        })

        # Broadcast release so viewers/agents can update claims state
        if isinstance(path, str) and isinstance(room, str) and actor is not None:
            from warroom.channel.protocol import Message, text_part
            ts = time.time()
            msg = Message(
                id=0, ts=ts, room=room, actor=actor,
                client_id=state.client_id,
                parts=[text_part(f"[system] {actor} released {path}")],
            )
            new_id = insert_message(self._db, msg)
            msg.id = new_id
            await self._broadcast(room, msg.to_dict(), exclude_client_id=state.client_id)

    async def _on_list_claims(self, state: ConnState, frame: dict[str, Any]) -> None:
        req_id = frame.get("req_id")
        room = frame.get("room", "room1")
        claims = [
            {"path": k[1], "actor": v[0], "claimed_at": v[1]}
            for k, v in self.file_claims.items()
            if k[0] == room
        ]
        await self._send(state, {
            "op": "claims_list", "reply_to_req_id": req_id,
            "ok": True, "claims": claims,
        })

    async def _on_history(self, state: ConnState, frame: dict[str, Any]) -> None:
        req_id = frame.get("req_id")
        room = frame.get("room", "room1")
        limit = min(int(frame.get("limit", 50)), 200)
        since_id = frame.get("since_id")

        if since_id is not None:
            msgs = fetch_since(self._db, room, int(since_id), limit=limit)
        else:
            msgs = fetch_history(self._db, room, limit=limit)

        await self._send(state, {
            "op": "history",
            "reply_to_req_id": req_id,
            "ok": True,
            "room": room,
            "messages": [m.to_dict() for m in msgs],
        })

    async def _on_room_state(self, state: ConnState, frame: dict[str, Any]) -> None:
        req_id = frame.get("req_id")
        room = frame.get("room", "room1")

        # Active agents
        active_agents = []
        for (r, actor), conn in self.active_joins.items():
            if r == room:
                active_agents.append({"actor": actor, "client_id": conn.client_id})

        # File claims
        claims = [
            {"path": k[1], "actor": v[0], "claimed_at": v[1]}
            for k, v in self.file_claims.items()
            if k[0] == room
        ]

        await self._send(state, {
            "op": "room_state",
            "reply_to_req_id": req_id,
            "ok": True,
            "room": room,
            "active_agents": active_agents,
            "claims": claims,
            "last_msg_id": self._last_msg_id(room),
        })

    async def expire_stale_claims(self) -> None:
        """Release claims older than CLAIM_TTL_S. Call periodically."""
        now = time.time()
        expired: list[tuple[str, str, str]] = []  # (room, path, actor)
        for (room, path), (actor, claimed_at) in list(self.file_claims.items()):
            if now - claimed_at > CLAIM_TTL_S:
                del self.file_claims[(room, path)]
                expired.append((room, path, actor))

        for room, path, actor in expired:
            # Broadcast expiry
            from warroom.channel.protocol import text_part
            ts = time.time()
            msg = Message(
                id=0, ts=ts, room=room, actor="system",
                client_id="system",
                parts=[text_part(f"[system] claim expired: {actor}'s lock on {path} (TTL {CLAIM_TTL_S}s)")],
            )
            new_id = insert_message(self._db, msg)
            msg.id = new_id
            await self._broadcast(room, msg.to_dict())

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
