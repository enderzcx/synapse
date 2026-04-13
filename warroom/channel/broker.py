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

from warroom.channel.db import fetch_history, fetch_mentions, fetch_since, insert_message
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
        # Task registry: {(room, task_id): task_dict} — structured task objects
        self.tasks: dict[tuple[str, str], dict[str, Any]] = {}
        self._task_counter = 0
        # Agent status: {(room, actor): status_dict} — heartbeat/presence
        self.agent_status: dict[tuple[str, str], dict[str, Any]] = {}
        # Last seen: {(room, actor): timestamp} — set on disconnect for offline mention tracking
        self.last_seen: dict[tuple[str, str], float] = {}

    # --- top-level entry points ---

    async def handle_frame(self, state: ConnState, frame: dict[str, Any]) -> None:
        op = frame.get("op")
        if op == FrameType.JOIN:
            await self._on_join(state, frame)
        elif op == FrameType.POST:
            await self._on_post(state, frame)
        elif op == FrameType.CONTROL:
            await self._on_control(state, frame)
        elif op == "claim_file":
            await self._on_claim_file(state, frame)
        elif op == "release_file":
            await self._on_release_file(state, frame)
        elif op == "list_claims":
            await self._on_list_claims(state, frame)
        elif op == "agent_status":
            await self._on_agent_status(state, frame)
        elif op == "task_create":
            await self._on_task_create(state, frame)
        elif op == "task_update":
            await self._on_task_update(state, frame)
        elif op == "task_get":
            await self._on_task_get(state, frame)
        elif op == "task_list":
            await self._on_task_list(state, frame)
        elif op == "task_handoff":
            await self._on_task_handoff(state, frame)
        elif op == "task_verdict":
            await self._on_task_verdict(state, frame)
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
                    # Record last_seen for offline mention tracking
                    self.last_seen[key] = time.time()
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

        # Offline mentions: messages that @actor or @all while agent was away
        mentions_dicts: list[dict[str, Any]] = []
        last_seen_ts = self.last_seen.get((room, actor))
        if last_seen_ts is not None:
            mentions = fetch_mentions(self._db, room, actor, last_seen_ts)
            mentions_dicts = [m.to_dict() for m in mentions]

        await self._send(state, {
            "op": FrameType.JOINED,
            "reply_to_req_id": req_id,
            "room": room,
            "last_msg_id": last_msg_id,
            "recent_messages": recent_dicts,
            "mentions": mentions_dicts,
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

    async def _on_control(self, state: ConnState, frame: dict[str, Any]) -> None:
        req_id = frame.get("req_id")
        room = frame.get("room")
        target = frame.get("target")
        action = frame.get("action")
        task_id = frame.get("task_id")
        data = frame.get("data")
        sender = state.actor

        if not (
            isinstance(room, str)
            and isinstance(target, str)
            and isinstance(action, str)
        ):
            await self._send(state, {
                "op": FrameType.ERROR,
                "reply_to_req_id": req_id,
                "code": "bad_request",
                "message": "control requires string room, target, action",
            })
            return

        if sender is None or room not in state.joined_rooms:
            await self._send(state, {
                "op": FrameType.ERROR,
                "reply_to_req_id": req_id,
                "code": "not_joined",
                "message": f"must join {room!r} before sending control",
            })
            return

        recipient = self.active_joins.get((room, target))
        if recipient is None:
            await self._send(state, {
                "op": FrameType.CONTROL_ACK,
                "reply_to_req_id": req_id,
                "ok": False,
                "room": room,
                "target": target,
                "action": action,
                "task_id": task_id if isinstance(task_id, str) else None,
                "code": "target_not_found",
                "message": f"target {target!r} is not joined in {room!r}",
            })
            return

        await self._send(state, {
            "op": FrameType.CONTROL_ACK,
            "reply_to_req_id": req_id,
            "ok": True,
            "room": room,
            "target": target,
            "action": action,
            "task_id": task_id if isinstance(task_id, str) else None,
        })
        await self._send(recipient, {
            "op": FrameType.CONTROL,
            "room": room,
            "target": target,
            "action": action,
            "task_id": task_id if isinstance(task_id, str) else None,
            "data": data,
            "from_actor": sender,
        })

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

    # --- agent status / heartbeat ---

    VALID_AGENT_PHASES = {"idle", "planning", "coding", "testing", "reviewing", "blocked", "waiting"}

    async def _on_agent_status(self, state: ConnState, frame: dict[str, Any]) -> None:
        req_id = frame.get("req_id")
        room = frame.get("room", "room1")
        actor = state.actor
        phase = frame.get("phase", "idle")
        task_id = frame.get("task_id")
        detail = frame.get("detail", "")

        if actor is None or room not in state.joined_rooms:
            await self._send(state, {
                "op": FrameType.ERROR, "reply_to_req_id": req_id,
                "code": "not_joined", "message": "must join room before reporting status",
            })
            return

        if phase not in self.VALID_AGENT_PHASES:
            await self._send(state, {
                "op": FrameType.ERROR, "reply_to_req_id": req_id,
                "code": "bad_request",
                "message": f"invalid phase {phase!r}, must be one of {self.VALID_AGENT_PHASES}",
            })
            return

        self.agent_status[(room, actor)] = {
            "actor": actor,
            "phase": phase,
            "task_id": task_id if isinstance(task_id, str) else None,
            "detail": str(detail)[:200],
            "updated_at": time.time(),
        }

        await self._send(state, {
            "op": "agent_status_ack", "reply_to_req_id": req_id,
            "ok": True, "actor": actor, "phase": phase,
        })

    # --- task registry ---

    VALID_TASK_STATUSES = {"todo", "doing", "review", "done", "blocked"}

    async def _on_task_create(self, state: ConnState, frame: dict[str, Any]) -> None:
        req_id = frame.get("req_id")
        room = frame.get("room", "room1")
        title = frame.get("title")
        goal = frame.get("goal", "")
        owner = frame.get("owner", state.actor or "unknown")
        reviewer = frame.get("reviewer", "")
        acceptance = frame.get("acceptance", [])
        write_set = frame.get("write_set", [])

        if not isinstance(title, str) or not title.strip():
            await self._send(state, {
                "op": FrameType.ERROR, "reply_to_req_id": req_id,
                "code": "bad_request", "message": "task_create requires non-empty title",
            })
            return

        self._task_counter += 1
        task_id = f"t-{self._task_counter:03d}"
        now = time.time()
        task = {
            "task_id": task_id,
            "room": room,
            "title": title,
            "goal": goal,
            "owner": owner,
            "reviewer": reviewer,
            "status": "todo",
            "acceptance": acceptance if isinstance(acceptance, list) else [],
            "write_set": write_set if isinstance(write_set, list) else [],
            "created_at": now,
            "updated_at": now,
        }
        self.tasks[(room, task_id)] = task

        await self._send(state, {
            "op": "task_created", "reply_to_req_id": req_id,
            "ok": True, "task": task,
        })

        # Broadcast task creation to room
        from warroom.channel.protocol import text_part
        msg = Message(
            id=0, ts=now, room=room, actor=state.actor or "unknown",
            client_id=state.client_id,
            parts=[text_part(f"[task] created {task_id}: {title} (owner: {owner})")],
        )
        new_id = insert_message(self._db, msg)
        msg.id = new_id
        await self._broadcast(room, msg.to_dict(), exclude_client_id=state.client_id)

    async def _on_task_update(self, state: ConnState, frame: dict[str, Any]) -> None:
        req_id = frame.get("req_id")
        room = frame.get("room", "room1")
        task_id = frame.get("task_id")

        if not isinstance(task_id, str):
            await self._send(state, {
                "op": FrameType.ERROR, "reply_to_req_id": req_id,
                "code": "bad_request", "message": "task_update requires task_id",
            })
            return

        task = self.tasks.get((room, task_id))
        if task is None:
            await self._send(state, {
                "op": FrameType.ERROR, "reply_to_req_id": req_id,
                "code": "not_found", "message": f"task {task_id!r} not found in {room!r}",
            })
            return

        # Apply allowed updates with gate enforcement
        new_status = frame.get("status")
        if new_status is not None:
            if new_status not in self.VALID_TASK_STATUSES:
                await self._send(state, {
                    "op": FrameType.ERROR, "reply_to_req_id": req_id,
                    "code": "bad_request",
                    "message": f"invalid status {new_status!r}, must be one of {self.VALID_TASK_STATUSES}",
                })
                return
            # Gate: review requires handoff first
            old_status = task["status"]
            if new_status == "review" and old_status == "doing" and not task.get("last_handoff"):
                await self._send(state, {
                    "op": FrameType.ERROR, "reply_to_req_id": req_id,
                    "code": "gate_blocked",
                    "message": "cannot move to review without submitting a handoff first",
                })
                return
            # Gate: done requires verdict(pass) first
            if new_status == "done" and old_status == "review":
                verdict = task.get("last_verdict")
                if not verdict or verdict.get("verdict") != "pass":
                    await self._send(state, {
                        "op": FrameType.ERROR, "reply_to_req_id": req_id,
                        "code": "gate_blocked",
                        "message": "cannot move to done without a passing verdict",
                    })
                    return
            task["status"] = new_status

        for field in ("owner", "reviewer", "goal"):
            val = frame.get(field)
            if isinstance(val, str):
                task[field] = val
        for field in ("acceptance", "write_set"):
            val = frame.get(field)
            if isinstance(val, list):
                task[field] = val

        task["updated_at"] = time.time()

        await self._send(state, {
            "op": "task_updated", "reply_to_req_id": req_id,
            "ok": True, "task": task,
        })

    async def _on_task_get(self, state: ConnState, frame: dict[str, Any]) -> None:
        req_id = frame.get("req_id")
        room = frame.get("room", "room1")
        task_id = frame.get("task_id")

        task = self.tasks.get((room, task_id)) if isinstance(task_id, str) else None
        if task is None:
            await self._send(state, {
                "op": FrameType.ERROR, "reply_to_req_id": req_id,
                "code": "not_found", "message": f"task {task_id!r} not found",
            })
            return

        await self._send(state, {
            "op": "task_detail", "reply_to_req_id": req_id,
            "ok": True, "task": task,
        })

    async def _on_task_list(self, state: ConnState, frame: dict[str, Any]) -> None:
        req_id = frame.get("req_id")
        room = frame.get("room", "room1")
        status_filter = frame.get("status")

        tasks = [
            t for (r, _), t in self.tasks.items()
            if r == room and (status_filter is None or t["status"] == status_filter)
        ]

        await self._send(state, {
            "op": "task_list_result", "reply_to_req_id": req_id,
            "ok": True, "tasks": tasks, "count": len(tasks),
        })

    async def _on_task_handoff(self, state: ConnState, frame: dict[str, Any]) -> None:
        req_id = frame.get("req_id")
        room = frame.get("room", "room1")
        task_id = frame.get("task_id")

        task = self.tasks.get((room, task_id)) if isinstance(task_id, str) else None
        if task is None:
            await self._send(state, {
                "op": FrameType.ERROR, "reply_to_req_id": req_id,
                "code": "not_found", "message": f"task {task_id!r} not found",
            })
            return

        handoff = {
            "from": state.actor or "unknown",
            "artifacts": frame.get("artifacts", []),
            "verified": frame.get("verified", []),
            "assumptions": frame.get("assumptions", []),
            "next_action": frame.get("next_action", ""),
            "ts": time.time(),
        }
        task["last_handoff"] = handoff
        task["status"] = "review"
        task["updated_at"] = time.time()

        await self._send(state, {
            "op": "task_handoff_ack", "reply_to_req_id": req_id,
            "ok": True, "task_id": task_id, "status": "review",
        })

        # Broadcast handoff notification
        from warroom.channel.protocol import text_part
        actor = state.actor or "unknown"
        msg = Message(
            id=0, ts=time.time(), room=room, actor=actor,
            client_id=state.client_id,
            parts=[text_part(
                f"[task] {actor} handed off {task_id}: "
                f"{', '.join(handoff['artifacts'][:3]) or 'no artifacts'}"
                f" -> review by {task.get('reviewer', '?')}"
            )],
        )
        new_id = insert_message(self._db, msg)
        msg.id = new_id
        await self._broadcast(room, msg.to_dict(), exclude_client_id=state.client_id)

    async def _on_task_verdict(self, state: ConnState, frame: dict[str, Any]) -> None:
        req_id = frame.get("req_id")
        room = frame.get("room", "room1")
        task_id = frame.get("task_id")
        verdict = frame.get("verdict")

        task = self.tasks.get((room, task_id)) if isinstance(task_id, str) else None
        if task is None:
            await self._send(state, {
                "op": FrameType.ERROR, "reply_to_req_id": req_id,
                "code": "not_found", "message": f"task {task_id!r} not found",
            })
            return

        if verdict not in ("pass", "fail", "needs_info"):
            await self._send(state, {
                "op": FrameType.ERROR, "reply_to_req_id": req_id,
                "code": "bad_request",
                "message": f"verdict must be pass|fail|needs_info, got {verdict!r}",
            })
            return

        verdict_obj = {
            "by": state.actor or "unknown",
            "verdict": verdict,
            "findings": frame.get("findings", []),
            "blocking": frame.get("blocking", verdict == "fail"),
            "ts": time.time(),
        }
        task["last_verdict"] = verdict_obj
        task["updated_at"] = time.time()

        # Status transition based on verdict
        if verdict == "pass":
            task["status"] = "done"
        elif verdict == "fail":
            task["status"] = "doing"
        elif verdict == "needs_info":
            task["status"] = "blocked"

        await self._send(state, {
            "op": "task_verdict_ack", "reply_to_req_id": req_id,
            "ok": True, "task_id": task_id,
            "verdict": verdict, "new_status": task["status"],
        })

        # Broadcast verdict notification
        from warroom.channel.protocol import text_part
        reviewer = state.actor or "unknown"
        msg = Message(
            id=0, ts=time.time(), room=room, actor=reviewer,
            client_id=state.client_id,
            parts=[text_part(
                f"[task] {reviewer} verdict on {task_id}: {verdict}"
                f"{' -- ' + '; '.join(verdict_obj['findings'][:3]) if verdict_obj['findings'] else ''}"
            )],
        )
        new_id = insert_message(self._db, msg)
        msg.id = new_id
        await self._broadcast(room, msg.to_dict(), exclude_client_id=state.client_id)

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

    def _compute_presence(self, room: str, actor: str) -> str:
        """Compute agent presence: online, away, or offline."""
        if (room, actor) not in self.active_joins:
            return "offline"
        status = self.agent_status.get((room, actor))
        if status and time.time() - status.get("updated_at", 0) > 300:
            return "away"
        return "online"

    async def _on_room_state(self, state: ConnState, frame: dict[str, Any]) -> None:
        req_id = frame.get("req_id")
        room = frame.get("room", "room1")

        # Active agents (with status and presence)
        active_agents = []
        for (r, actor), conn in self.active_joins.items():
            if r == room:
                agent_info: dict[str, Any] = {
                    "actor": actor,
                    "client_id": conn.client_id,
                    "presence": self._compute_presence(room, actor),
                }
                status = self.agent_status.get((room, actor))
                if status:
                    agent_info["phase"] = status["phase"]
                    agent_info["task_id"] = status.get("task_id")
                    agent_info["detail"] = status.get("detail", "")
                    agent_info["status_updated_at"] = status.get("updated_at")
                active_agents.append(agent_info)

        # Include recently seen offline agents
        for (r, actor), seen_ts in self.last_seen.items():
            if r == room and (room, actor) not in self.active_joins:
                active_agents.append({
                    "actor": actor,
                    "presence": "offline",
                    "last_seen": seen_ts,
                })

        # File claims
        claims = [
            {"path": k[1], "actor": v[0], "claimed_at": v[1]}
            for k, v in self.file_claims.items()
            if k[0] == room
        ]

        # Active tasks
        active_tasks = [
            {"task_id": t["task_id"], "title": t["title"], "owner": t["owner"], "status": t["status"]}
            for (r, _), t in self.tasks.items()
            if r == room and t["status"] not in ("done",)
        ]

        await self._send(state, {
            "op": "room_state",
            "reply_to_req_id": req_id,
            "ok": True,
            "room": room,
            "active_agents": active_agents,
            "claims": claims,
            "tasks": active_tasks,
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
