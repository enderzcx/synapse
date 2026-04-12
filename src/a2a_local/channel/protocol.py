"""A2A Channel wire protocol: frames and messages.

Design reference: docs/phase2-channel-design.md v4.

Wire format is JSON over WebSocket. Client → Server frames carry `req_id`
for request correlation; Server → Client response frames carry
`reply_to_req_id` matching that id. Broadcast frames (unsolicited) carry
neither — they are pushed to every room subscriber except the sender.

The single reader task on each client distinguishes:
  - frames with `reply_to_req_id` → route to pending Future by req_id
  - frames with `op == "broadcast"` → push to broadcast asyncio.Queue

See ws_client.ChannelClient for the reader/demux implementation.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


class FrameType:
    """String constants for known frame ops.

    Not a real enum to keep JSON serialization trivial: every frame carries
    its op as a plain string matching these values.
    """

    # Client → Server
    JOIN = "join"
    POST = "post"
    PING = "ping"

    # Server → Client (response, carries reply_to_req_id)
    JOINED = "joined"
    POSTED = "posted"
    PONG = "pong"
    ERROR = "error"

    # Server → Client (unsolicited)
    BROADCAST = "broadcast"


@dataclass
class Message:
    """A single chat message in a channel room.

    Fields map 1:1 to the `messages` SQLite table. `id` is assigned by the
    broker on insert; clients never generate it.
    """

    id: int
    ts: float
    room: str
    actor: str
    client_id: str
    content: str
    reply_to: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        return cls(
            id=int(d["id"]),
            ts=float(d["ts"]),
            room=str(d["room"]),
            actor=str(d["actor"]),
            client_id=str(d["client_id"]),
            content=str(d["content"]),
            reply_to=d.get("reply_to"),
        )


@dataclass
class Frame:
    """One WebSocket wire frame.

    A frame carries only the fields relevant to its `op`; unused fields stay
    None and are stripped from the serialized JSON by `encode_frame` to keep
    the wire small and debug-friendly.
    """

    op: str
    req_id: str | None = None
    reply_to_req_id: str | None = None

    # join
    room: str | None = None
    actor: str | None = None
    client_id: str | None = None

    # post
    content: str | None = None
    reply_to: int | None = None

    # joined / posted response
    last_msg_id: int | None = None
    msg_id: int | None = None
    ts: float | None = None
    ok: bool | None = None

    # broadcast
    msg: dict[str, Any] | None = None

    # error
    code: str | None = None
    message: str | None = None


def encode_frame(frame: Frame) -> str:
    """Serialize a Frame to JSON string, dropping None-valued fields."""
    d = {k: v for k, v in asdict(frame).items() if v is not None}
    return json.dumps(d, separators=(",", ":"))


def decode_frame(raw: str) -> Frame:
    """Parse a JSON wire frame. Raises ValueError on malformed input."""
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid json frame: {e}") from e
    if not isinstance(d, dict):
        raise ValueError(f"frame must be object, got {type(d).__name__}")
    op = d.get("op")
    if not isinstance(op, str) or not op:
        raise ValueError("frame missing required 'op' field")
    # Build Frame with only known fields; unknown fields silently ignored.
    known_fields = {f.name for f in Frame.__dataclass_fields__.values()}
    kwargs = {k: v for k, v in d.items() if k in known_fields}
    return Frame(**kwargs)
