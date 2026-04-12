"""SQLite persistence for channel messages.

WAL mode + busy_timeout = concurrent readers OK, writers serialized.
Broker uses a single connection in its asyncio loop so there's no
real concurrency — WAL is defense in depth for viewer/reader tools
that might want to open the same db for inspection.
"""
from __future__ import annotations

import sqlite3

from a2a_local.channel.protocol import Message


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    room      TEXT    NOT NULL,
    ts        REAL    NOT NULL,
    actor     TEXT    NOT NULL,
    client_id TEXT    NOT NULL,
    content   TEXT    NOT NULL,
    reply_to  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_room_id ON messages(room, id);
"""


def init_db(path: str) -> sqlite3.Connection:
    """Open (or create) the SQLite db at `path` and ensure schema exists.

    `path` can be ":memory:" for ephemeral test dbs.
    Returns a connection configured for WAL mode.
    """
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit
    # WAL + busy_timeout: safe for concurrent reads, writes serialized
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    return conn


def insert_message(conn: sqlite3.Connection, msg: Message) -> int:
    """Insert a message; returns the new autoincrement id.

    `msg.id` on input is ignored (always 0 from fresh Messages).
    """
    cur = conn.execute(
        """
        INSERT INTO messages (room, ts, actor, client_id, content, reply_to)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (msg.room, msg.ts, msg.actor, msg.client_id, msg.content, msg.reply_to),
    )
    new_id = cur.lastrowid
    assert new_id is not None
    return int(new_id)


def fetch_since(
    conn: sqlite3.Connection,
    room: str,
    since_id: int,
    limit: int = 50,
) -> list[Message]:
    """Return messages in `room` with id > since_id, ascending by id, up to limit."""
    cur = conn.execute(
        """
        SELECT id, ts, room, actor, client_id, content, reply_to
        FROM messages
        WHERE room = ? AND id > ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (room, since_id, limit),
    )
    return [_row_to_message(row) for row in cur.fetchall()]


def fetch_history(
    conn: sqlite3.Connection,
    room: str,
    limit: int = 50,
) -> list[Message]:
    """Return the LAST `limit` messages in room, ascending by id.

    v5 MED 3 fix: the previous impl selected the oldest N rows (ASC LIMIT),
    contradicting the docstring. We now grab the tail (DESC LIMIT in a
    subquery) and re-order ascending in the outer query so callers still
    get chronological order.
    """
    cur = conn.execute(
        """
        SELECT id, ts, room, actor, client_id, content, reply_to
        FROM (
            SELECT id, ts, room, actor, client_id, content, reply_to
            FROM messages
            WHERE room = ?
            ORDER BY id DESC
            LIMIT ?
        )
        ORDER BY id ASC
        """,
        (room, limit),
    )
    return [_row_to_message(row) for row in cur.fetchall()]


def _row_to_message(row: tuple) -> Message:
    return Message(
        id=int(row[0]),
        ts=float(row[1]),
        room=str(row[2]),
        actor=str(row[3]),
        client_id=str(row[4]),
        content=str(row[5]),
        reply_to=None if row[6] is None else int(row[6]),
    )
