"""Step 2 RED: SQLite schema + CRUD for channel messages."""
import time

import pytest

from warroom.channel.db import (
    fetch_history,
    fetch_since,
    init_db,
    insert_message,
)
from warroom.channel.protocol import Message, text_part


@pytest.fixture
def conn():
    c = init_db(":memory:")
    yield c
    c.close()


def _mk(room: str = "r1", actor: str = "claude", content: str = "hi",
        ts: float | None = None, reply_to: int | None = None) -> Message:
    return Message(
        id=0,
        ts=ts if ts is not None else time.time(),
        room=room,
        actor=actor,
        client_id="cid-" + actor,
        parts=[text_part(content)],
        reply_to=reply_to,
    )


def test_init_db_creates_schema(conn):
    cur = conn.execute("select name from sqlite_master where type='table'")
    tables = {row[0] for row in cur.fetchall()}
    assert "messages" in tables


def test_insert_message_returns_autoincrement_id(conn):
    m1 = _mk(content="first")
    m2 = _mk(content="second")
    id1 = insert_message(conn, m1)
    id2 = insert_message(conn, m2)
    assert id1 >= 1
    assert id2 > id1


def test_fetch_history_returns_ordered(conn):
    insert_message(conn, _mk(content="a", ts=1.0))
    insert_message(conn, _mk(content="b", ts=2.0))
    insert_message(conn, _mk(content="c", ts=3.0))
    rows = fetch_history(conn, room="r1", limit=50)
    assert [r.content for r in rows] == ["a", "b", "c"]


def test_fetch_since_filters(conn):
    i1 = insert_message(conn, _mk(content="a"))
    i2 = insert_message(conn, _mk(content="b"))
    i3 = insert_message(conn, _mk(content="c"))
    rows = fetch_since(conn, room="r1", since_id=i1, limit=50)
    assert [r.content for r in rows] == ["b", "c"]
    assert [r.id for r in rows] == [i2, i3]


def test_fetch_since_limit(conn):
    for i in range(5):
        insert_message(conn, _mk(content=f"m{i}"))
    rows = fetch_since(conn, room="r1", since_id=0, limit=2)
    assert len(rows) == 2


def test_multi_room_isolation(conn):
    insert_message(conn, _mk(room="r1", content="in r1"))
    insert_message(conn, _mk(room="r2", content="in r2"))
    r1 = fetch_history(conn, room="r1", limit=50)
    r2 = fetch_history(conn, room="r2", limit=50)
    assert len(r1) == 1 and r1[0].content == "in r1"
    assert len(r2) == 1 and r2[0].content == "in r2"


def test_insert_preserves_all_fields(conn):
    m = _mk(actor="codex", content="@claude review this", reply_to=7, ts=99.5)
    new_id = insert_message(conn, m)
    rows = fetch_history(conn, room="r1", limit=50)
    assert len(rows) == 1
    r = rows[0]
    assert r.id == new_id
    assert r.actor == "codex"
    assert r.content == "@claude review this"
    assert r.reply_to == 7
    assert r.ts == 99.5
    assert r.client_id == "cid-codex"


def test_fetch_history_empty_room_returns_empty(conn):
    assert fetch_history(conn, room="empty", limit=50) == []


def test_fetch_history_returns_tail_not_head(conn):
    """v5 MED 3 regression: fetch_history must return the LAST N messages
    (by id), in chronological order. Previously it returned the FIRST N
    due to ORDER BY id ASC LIMIT, contradicting its docstring."""
    for i in range(5):
        insert_message(conn, _mk(content=f"m{i}", ts=float(i)))
    rows = fetch_history(conn, room="r1", limit=2)
    assert [r.content for r in rows] == ["m3", "m4"]
    # Still ascending order
    assert rows[0].id < rows[1].id
