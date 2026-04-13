"""A2A Channel wire protocol: frames and messages.

Wire format is JSON over WebSocket. Messages use the A2A standard format
(Message with Parts array) for interoperability with any A2A-compatible agent.

Client → Server frames carry `req_id` for request correlation.
Server → Client response frames carry `reply_to_req_id`.
Broadcast frames (unsolicited) have neither.

A2A message format reference:
  - role: "agent" | "user"
  - parts: [{"kind": "text", "text": "..."}, ...]
  - messageId: UUID hex string

The `content` field is kept as a convenience alias that reads/writes
the first TextPart. This means existing MCP tools can still do
`channel_post(content="hello")` without knowing about parts.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


class FrameType:
    """String constants for known frame ops."""

    # Client → Server
    JOIN = "join"
    POST = "post"
    PING = "ping"
    CONTROL = "control"

    # Server → Client (response, carries reply_to_req_id)
    JOINED = "joined"
    POSTED = "posted"
    PONG = "pong"
    CONTROL_ACK = "control_ack"
    ERROR = "error"

    # Server → Client (unsolicited)
    BROADCAST = "broadcast"


# --- A2A-compatible message parts ---

def text_part(text: str) -> dict[str, Any]:
    """Create an A2A TextPart dict."""
    return {"kind": "text", "text": text}


def file_part(file_uri: str, mime_type: str = "text/plain",
              name: str | None = None) -> dict[str, Any]:
    """Create an A2A FilePart dict."""
    p: dict[str, Any] = {"kind": "file", "file": {"uri": file_uri, "mimeType": mime_type}}
    if name:
        p["file"]["name"] = name
    return p


def data_part(data: Any, mime_type: str = "application/json") -> dict[str, Any]:
    """Create an A2A DataPart dict."""
    return {"kind": "data", "data": data, "metadata": {"mimeType": mime_type}}


# --- Message ---

@dataclass
class Message:
    """A channel message using A2A standard format.

    Core fields (stored in SQLite):
      id, ts, room, actor, client_id, role, parts, reply_to

    The `content` property is a convenience that extracts/sets the first
    TextPart's text. This keeps the MCP shim API simple: agents just pass
    `content="hello"` and it auto-wraps into `[{"kind":"text","text":"hello"}]`.

    The `message_id` field is an A2A-standard UUID for deduplication.
    """

    id: int
    ts: float
    room: str
    actor: str
    client_id: str
    role: str = "agent"  # A2A Role: "agent" | "user"
    parts: list[dict[str, Any]] = field(default_factory=list)
    message_id: str = ""  # A2A messageId (UUID hex)
    reply_to: int | None = None

    def __post_init__(self) -> None:
        if not self.message_id:
            self.message_id = uuid.uuid4().hex

    @property
    def content(self) -> str:
        """Extract text from the first TextPart, or empty string."""
        for part in self.parts:
            if isinstance(part, dict) and part.get("kind") == "text":
                return part.get("text", "")
        return ""

    @content.setter
    def content(self, value: str) -> None:
        """Set the first TextPart's text, creating one if needed."""
        for part in self.parts:
            if isinstance(part, dict) and part.get("kind") == "text":
                part["text"] = value
                return
        self.parts.insert(0, text_part(value))

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "ts": self.ts,
            "room": self.room,
            "actor": self.actor,
            "client_id": self.client_id,
            "role": self.role,
            "parts": self.parts,
            "messageId": self.message_id,
        }
        if self.reply_to is not None:
            d["reply_to"] = self.reply_to
        # Convenience: include flat "content" for backward compat with
        # simple consumers that don't parse parts
        d["content"] = self.content
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        # Support both A2A parts and legacy flat content.
        # If parts is missing, empty, or invalid BUT content exists, fall back
        # to wrapping content into a TextPart (prevents silent text loss).
        raw_parts = d.get("parts")
        content_str = d.get("content", "")

        if isinstance(raw_parts, list) and len(raw_parts) > 0:
            parts = raw_parts
        elif content_str:
            # Legacy or hybrid with empty/invalid parts: wrap content
            parts = [text_part(str(content_str))]
        else:
            parts = []

        return cls(
            id=int(d.get("id", 0)),
            ts=float(d.get("ts", 0)),
            room=str(d.get("room", "")),
            actor=str(d.get("actor", "")),
            client_id=str(d.get("client_id", "")),
            role=str(d.get("role", "agent")),
            parts=parts if isinstance(parts, list) else [],
            message_id=str(d.get("messageId", d.get("message_id", ""))),
            reply_to=d.get("reply_to"),
        )


# --- Frame ---

@dataclass
class Frame:
    """One WebSocket wire frame.

    A frame carries only the fields relevant to its `op`; unused fields stay
    None and are stripped from the serialized JSON by `encode_frame`.
    """

    op: str
    req_id: str | None = None
    reply_to_req_id: str | None = None

    # join
    room: str | None = None
    actor: str | None = None
    client_id: str | None = None

    # post — now supports A2A parts
    content: str | None = None  # convenience (auto-wrapped to TextPart by broker)
    parts: list[dict[str, Any]] | None = None  # A2A parts array
    role: str | None = None  # A2A role
    reply_to: int | None = None

    # joined / posted response
    last_msg_id: int | None = None
    msg_id: int | None = None
    ts: float | None = None
    ok: bool | None = None

    # control
    target: str | None = None
    action: str | None = None
    task_id: str | None = None
    data: Any | None = None
    from_actor: str | None = None

    # broadcast
    msg: dict[str, Any] | None = None

    # error
    code: str | None = None
    message: str | None = None


def encode_frame(frame: Frame) -> str:
    """Serialize a Frame to JSON string, dropping None-valued fields."""
    d: dict[str, Any] = {}
    for k, v in asdict(frame).items():
        if v is not None:
            d[k] = v
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
    known_fields = {f.name for f in Frame.__dataclass_fields__.values()}
    kwargs = {k: v for k, v in d.items() if k in known_fields}
    return Frame(**kwargs)
