"""Step 1 RED: frame schema + serialization for A2A channel protocol."""
import json

import pytest

from warroom.channel.protocol import (
    Frame,
    FrameType,
    Message,
    decode_frame,
    encode_frame,
    text_part,
)


# --- Message dataclass (A2A format) ---

def test_message_round_trip():
    m = Message(
        id=42,
        ts=1775912345.123,
        room="room1",
        actor="claude",
        client_id="abc-123",
        parts=[text_part("hello world")],
        reply_to=None,
    )
    d = m.to_dict()
    assert d["id"] == 42
    assert d["ts"] == 1775912345.123
    assert d["content"] == "hello world"  # convenience flat field
    assert d["parts"][0]["kind"] == "text"
    assert d["parts"][0]["text"] == "hello world"
    assert d["role"] == "agent"
    assert "messageId" in d
    m2 = Message.from_dict(d)
    assert m2.content == m.content
    assert m2.parts == m.parts


def test_message_with_reply_to():
    m = Message(id=2, ts=1.0, room="r", actor="a", client_id="c",
                parts=[text_part("x")], reply_to=1)
    assert m.to_dict()["reply_to"] == 1


def test_message_content_property():
    """content property reads/writes the first TextPart."""
    m = Message(id=0, ts=0, room="r", actor="a", client_id="c", parts=[])
    assert m.content == ""
    m.content = "hello"
    assert m.content == "hello"
    assert m.parts[0] == {"kind": "text", "text": "hello"}


def test_message_from_legacy_content():
    """from_dict with plain 'content' string (no parts) auto-wraps into TextPart."""
    d = {"id": 1, "ts": 1.0, "room": "r", "actor": "a", "client_id": "c",
         "content": "legacy"}
    m = Message.from_dict(d)
    assert m.content == "legacy"
    assert m.parts == [{"kind": "text", "text": "legacy"}]


# --- Request frames (client → server) ---

def test_join_request_encode():
    f = Frame(
        op="join",
        req_id="req-1",
        room="room1",
        actor="claude",
        client_id="cid-1",
    )
    raw = encode_frame(f)
    parsed = json.loads(raw)
    assert parsed["op"] == "join"
    assert parsed["req_id"] == "req-1"
    assert parsed["room"] == "room1"
    assert parsed["actor"] == "claude"
    assert parsed["client_id"] == "cid-1"


def test_post_request_encode():
    f = Frame(
        op="post",
        req_id="req-2",
        room="room1",
        client_id="cid-1",
        content="hi",
        reply_to=None,
    )
    parsed = json.loads(encode_frame(f))
    assert parsed["op"] == "post"
    assert parsed["content"] == "hi"


# --- Response frames (server → client, with reply_to_req_id) ---

def test_joined_response_decode():
    raw = json.dumps({
        "op": "joined",
        "reply_to_req_id": "req-1",
        "room": "room1",
        "last_msg_id": 42,
        "ok": True,
    })
    f = decode_frame(raw)
    assert f.op == "joined"
    assert f.reply_to_req_id == "req-1"
    assert f.room == "room1"
    assert f.last_msg_id == 42
    assert f.ok is True


def test_error_response_decode():
    raw = json.dumps({
        "op": "error",
        "reply_to_req_id": "req-x",
        "code": "duplicate_actor",
        "message": "claude already joined room1",
    })
    f = decode_frame(raw)
    assert f.op == "error"
    assert f.code == "duplicate_actor"
    assert f.message == "claude already joined room1"


def test_control_frame_round_trip():
    raw = json.dumps({
        "op": "control",
        "room": "room1",
        "target": "codex",
        "action": "interrupt",
        "task_id": "task-1",
        "data": {"reason": "user_override"},
        "from_actor": "claude",
    })
    f = decode_frame(raw)
    assert f.op == "control"
    assert f.room == "room1"
    assert f.target == "codex"
    assert f.action == "interrupt"
    assert f.task_id == "task-1"
    assert f.data == {"reason": "user_override"}
    assert f.from_actor == "claude"


# --- Broadcast frame (server → client, NO reply_to_req_id) ---

def test_broadcast_frame_decode():
    raw = json.dumps({
        "op": "broadcast",
        "room": "room1",
        "msg": {
            "id": 43,
            "ts": 1775912345.12,
            "room": "room1",
            "actor": "claude",
            "client_id": "cid-1",
            "content": "text",
            "reply_to": None,
        },
    })
    f = decode_frame(raw)
    assert f.op == "broadcast"
    assert f.reply_to_req_id is None
    assert f.msg is not None
    m = Message.from_dict(f.msg)
    assert m.id == 43
    assert m.actor == "claude"


# --- Error cases ---

def test_decode_invalid_json_raises():
    with pytest.raises(ValueError):
        decode_frame("{not json}")


def test_decode_missing_op_raises():
    with pytest.raises(ValueError):
        decode_frame(json.dumps({"req_id": "x"}))


def test_frame_type_enum_has_expected_values():
    # sanity: known ops can be identified
    assert FrameType.JOIN == "join"
    assert FrameType.POST == "post"
    assert FrameType.CONTROL == "control"
    assert FrameType.JOINED == "joined"
    assert FrameType.POSTED == "posted"
    assert FrameType.CONTROL_ACK == "control_ack"
    assert FrameType.BROADCAST == "broadcast"
    assert FrameType.ERROR == "error"
