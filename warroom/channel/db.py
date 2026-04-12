"""SQLite persistence for channel messages (A2A-compatible format).

Messages store A2A `parts` as JSON and `role` as string.
The `content` column is kept as a denormalized convenience field
(first TextPart's text) for simple queries and backward compat.

WAL mode + busy_timeout = concurrent readers OK, writers serialized.
"""
from __future__ import annotations

import json
import sqlite3

from warroom.channel.protocol import Message


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    room       TEXT    NOT NULL,
    ts         REAL    NOT NULL,
    actor      TEXT    NOT NULL,
    client_id  TEXT    NOT NULL,
    role       TEXT    NOT NULL DEFAULT 'agent',
    parts      TEXT    NOT NULL DEFAULT '[]',
    message_id TEXT    NOT NULL DEFAULT '',
    content    TEXT    NOT NULL DEFAULT '',
    reply_to   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_room_id ON messages(room, id);
"""


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    # Migrate legacy tables that lack A2A columns
    _migrate_add_columns(conn)
    return conn


def _migrate_add_columns(conn: sqlite3.Connection) -> None:
    """Add role/parts/message_id/content columns if missing (legacy DB upgrade)."""
    cur = conn.execute("PRAGMA table_info(messages)")
    existing = {row[1] for row in cur.fetchall()}
    migrations = [
        ("role", "TEXT NOT NULL DEFAULT 'agent'"),
        ("parts", "TEXT NOT NULL DEFAULT '[]'"),
        ("message_id", "TEXT NOT NULL DEFAULT ''"),
    ]
    if "content" not in existing:
        migrations.append(("content", "TEXT NOT NULL DEFAULT ''"))
    for col, typedef in migrations:
        if col not in existing:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {typedef}")
    # Backfill parts from legacy content for rows that have content but empty parts
    if "parts" not in existing and "content" in existing:
        conn.execute(
            """UPDATE messages SET parts = '[{"kind":"text","text":' ||
               json_quote(content) || '}]' WHERE content != '' AND parts = '[]'"""
        )


def insert_message(conn: sqlite3.Connection, msg: Message) -> int:
    parts_json = json.dumps(msg.parts, separators=(",", ":"), ensure_ascii=False)
    cur = conn.execute(
        """
        INSERT INTO messages (room, ts, actor, client_id, role, parts, message_id, content, reply_to)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (msg.room, msg.ts, msg.actor, msg.client_id,
         msg.role, parts_json, msg.message_id, msg.content, msg.reply_to),
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
    cur = conn.execute(
        """
        SELECT id, ts, room, actor, client_id, role, parts, message_id, content, reply_to
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
    cur = conn.execute(
        """
        SELECT id, ts, room, actor, client_id, role, parts, message_id, content, reply_to
        FROM (
            SELECT id, ts, room, actor, client_id, role, parts, message_id, content, reply_to
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
    parts_raw = row[6]
    try:
        parts = json.loads(parts_raw) if parts_raw else []
    except (json.JSONDecodeError, TypeError):
        parts = []

    return Message(
        id=int(row[0]),
        ts=float(row[1]),
        room=str(row[2]),
        actor=str(row[3]),
        client_id=str(row[4]),
        role=str(row[5]) if row[5] else "agent",
        parts=parts if isinstance(parts, list) else [],
        message_id=str(row[7]) if row[7] else "",
        reply_to=None if row[9] is None else int(row[9]),
    )
