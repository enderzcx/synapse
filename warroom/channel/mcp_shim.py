"""MCP stdio server exposing channel tools to Claude Code / Codex CLI.

Wraps ChannelClient into 3 MCP tools:
  - channel_join(room)
  - channel_post(content, room, reply_to?)
  - channel_wait_new(room, timeout_s)

The shim maintains a single ChannelClient instance for its lifetime.
It connects to the broker on first tool call (lazy) and stays connected.

Two-phase bootstrap signal (v4 design):
  - On successful join: broadcasts system msg "<actor> joined <room>"
  - On first channel_wait_new call: broadcasts "<actor> listening"
    BEFORE blocking, so the viewer can confirm the agent entered the loop.

Usage:
    uv run python -m warroom.channel.mcp_shim \
        --actor claude --broker ws://127.0.0.1:9100

Registered in .mcp.json or via `codex mcp add`.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from mcp.server.fastmcp import FastMCP

from warroom.channel.ws_client import ChannelClient

# Redirect all logging to stderr so MCP stdio frames on stdout stay clean.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("a2a.channel.shim")

# --- globals set by __main__ ---
_actor: str = "unknown"
_broker_url: str = "ws://127.0.0.1:9100"
_client: ChannelClient | None = None
_listening_announced: dict[str, bool] = {}  # room -> bool

# Phase B probe result: Codex CLI hard limit = 120s, safe = 109s.
# We default to 60s for faster channel response; can raise up to ~100s.
DEFAULT_TIMEOUT_S = 60.0

mcp = FastMCP("channel")


async def _ensure_client() -> ChannelClient:
    global _client
    if _client is None:
        _client = ChannelClient(_broker_url, actor=_actor)
        await _client.connect()
    return _client


@mcp.tool()
async def channel_join(room: str = "room1") -> dict:
    """Join a channel room. Must call this BEFORE post or wait_new.

    Returns {"ok": true, "room": ..., "last_msg_id": int} on success.

    After joining, the shim broadcasts a system message "<actor> joined <room>"
    so the viewer and other participants can see you arrived.
    """
    client = await _ensure_client()
    resp = await client.join(room)
    # Broadcast system join notification
    try:
        await client.post(room, content=f"[system] {_actor} joined {room}")
    except Exception:
        pass  # non-fatal
    return {"ok": True, "room": room, "last_msg_id": resp.get("last_msg_id", 0)}


@mcp.tool()
async def channel_post(
    content: str,
    room: str = "room1",
    reply_to: int | None = None,
) -> dict:
    """Post a message to the channel visible to all participants.

    USE to send findings, code, questions, or replies to other agents.
    Other participants (Claude, Codex, user in viewer) will see this
    message in real time.

    Returns {"ok": true, "msg_id": int, "ts": float}.
    """
    client = await _ensure_client()
    resp = await client.post(room, content=content, reply_to=reply_to)
    return {
        "ok": True,
        "msg_id": resp.get("msg_id"),
        "ts": resp.get("ts"),
    }


@mcp.tool()
async def channel_wait_new(
    room: str = "room1",
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict:
    """Block until a new message from ANOTHER participant arrives, or timeout.

    USE PROACTIVELY in listening loop:
      1. Call channel_wait_new — blocks until someone else posts
      2. Process the returned message as a normal task (read files, write
         code, think, call tools — whatever the message asks for)
      3. Call channel_post with your reply
      4. Call channel_wait_new again
      5. If timed_out=true, just call channel_wait_new again immediately
         (unless the user told you to stop listening)

    The loop exits ONLY when the user interrupts you (Esc / Ctrl+C).

    Returns:
      {"ok": true, "msg": {"id":..., "actor":..., "content":..., ...}}
        when a message arrives.
      {"ok": true, "timed_out": true}
        when timeout expires with no new messages — call again.
    """
    client = await _ensure_client()
    # v4 two-phase bootstrap: announce "listening" on FIRST wait_new call
    if not _listening_announced.get(room, False):
        _listening_announced[room] = True
        try:
            await client.post(room, content=f"[system] {_actor} listening")
        except Exception:
            pass  # non-fatal

    try:
        msg = await client.wait_new(room, timeout_s=timeout_s)
    except ConnectionError as e:
        return {"ok": False, "error": str(e)}

    if msg is None:
        return {"ok": True, "timed_out": True}
    return {"ok": True, "msg": msg}


def main() -> None:
    global _actor, _broker_url

    parser = argparse.ArgumentParser(description="A2A channel MCP shim")
    parser.add_argument("--actor", required=True, help="Actor name (claude/codex/user)")
    parser.add_argument("--broker", default="ws://127.0.0.1:9100", help="Broker WebSocket URL")
    args = parser.parse_args()
    _actor = args.actor
    _broker_url = args.broker
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
