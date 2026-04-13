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
import os
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
_repo_root: str = ""  # explicit repo root for git ops (MED 2 fix)
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
    is_reconnect = resp.get("is_reconnect", False)
    # Broadcast appropriate system notification
    try:
        if is_reconnect:
            await client.post(room, content=f"[system] {_actor} reconnected to {room}")
        else:
            await client.post(room, content=f"[system] {_actor} joined {room}")
    except Exception:
        pass  # non-fatal
    return {"ok": True, "room": room, "last_msg_id": resp.get("last_msg_id", 0), "is_reconnect": is_reconnect}


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


@mcp.tool()
async def git_status() -> dict:
    """Show current git branch, modified files, and commits ahead of main.

    Check this before and after editing files to stay aware of repo state.
    Returns {"ok": true, "branch": "main", "modified": [...], "staged": [...], "commits_ahead": 0}.
    """
    from warroom.channel.git_ops import get_status
    return await get_status(cwd=_repo_root or os.getcwd())


@mcp.tool()
async def git_commit(message: str) -> dict:
    """Stage all changes and commit with the given message.

    NON-BLOCKING: returns immediately with a job_id. The commit runs in
    the background. When it finishes, the result is posted to the channel
    automatically. You can also check status with git_job_status(job_id).

    Returns {"ok": true, "job_id": "abc123", "status": "queued"}.
    """
    from warroom.channel.git_ops import submit_commit_job

    cwd = _repo_root or os.getcwd()

    async def _notify_channel(job_id: str, result: dict) -> None:
        """Post commit result back to the channel."""
        try:
            client = await _ensure_client()
            if result.get("ok"):
                text = (
                    f"[git] commit `{result['commit']}` on `{result.get('branch', '?')}` "
                    f"— {len(result.get('files', []))} file(s): {result['message']}"
                )
            else:
                step = result.get("step", "?")
                text = f"[git] commit failed at step `{step}`: {result.get('error', 'unknown')}"
            await client.post("room1", content=text)
        except Exception:
            pass  # best-effort notification

    job_id = submit_commit_job(
        message=message, cwd=cwd, on_complete=_notify_channel
    )
    return {"ok": True, "job_id": job_id, "status": "queued"}


@mcp.tool()
async def git_job_status(job_id: str) -> dict:
    """Check the status of a background git commit job.

    Returns {"ok": true, "job_id": "...", "status": "queued|running|succeeded|failed", "result": {...}}.
    If the job_id is unknown, returns {"ok": false, "error": "unknown job_id: ..."}.
    """
    from warroom.channel.git_ops import get_job_status
    return get_job_status(job_id)


@mcp.tool()
async def channel_claim_file(path: str, room: str = "room1") -> dict:
    """Declare intent to modify a file. Other agents will be blocked from
    claiming the same file until you release it.

    Call BEFORE editing any file. If another agent already claimed this path,
    you get an error — coordinate via channel_post before proceeding.

    Returns {"ok": true, "path": "auth.py"} on success, or error with
    "file_conflict" code if another agent holds the claim.
    """
    client = await _ensure_client()
    return await client._request("claim_file", room=room, path=path)


@mcp.tool()
async def channel_release_file(path: str, room: str = "room1") -> dict:
    """Release your claim on a file after you're done editing it.

    Always release files after committing your changes so other agents
    can work on them.

    Returns {"ok": true, "path": "auth.py"}.
    """
    client = await _ensure_client()
    return await client._request("release_file", room=room, path=path)


@mcp.tool()
async def channel_list_claims(room: str = "room1") -> dict:
    """List all currently claimed files in the room.

    Returns {"ok": true, "claims": [{"path": "auth.py", "actor": "claude"}, ...]}.
    Check this before starting work to see what files are already being edited.
    """
    client = await _ensure_client()
    return await client._request("list_claims", room=room)


@mcp.tool()
async def channel_peek_inbox(room: str = "room1") -> dict:
    """Non-blocking check for new messages in your local inbox.

    USE during long tasks (after editing a file, running a test, etc.)
    to check if someone sent you a message while you were working.
    Returns immediately -- never blocks or contacts the broker.

    Returns {"ok": true, "messages": [...], "count": N}.
    Empty list means no new messages. Messages returned here will NOT
    appear in subsequent channel_wait_new calls.
    """
    client = await _ensure_client()
    messages = client.peek_new(room)
    return {"ok": True, "messages": messages, "count": len(messages)}


@mcp.tool()
async def channel_history(
    room: str = "room1",
    limit: int = 20,
    since_id: int | None = None,
) -> dict:
    """Fetch recent message history from a channel room.

    USE after joining a room to catch up on what happened while you were away.
    Returns messages in chronological order (oldest first).

    Args:
        room: Room name
        limit: Max messages to return (1-200, default 20)
        since_id: If provided, return messages after this ID (incremental fetch)

    Returns {"ok": true, "room": "room1", "messages": [...]}.
    """
    client = await _ensure_client()
    req: dict = {"room": room, "limit": min(limit, 200)}
    if since_id is not None:
        req["since_id"] = since_id
    return await client._request("history", **req)


@mcp.tool()
async def channel_state(room: str = "room1") -> dict:
    """Get a snapshot of the current room state.

    USE after joining to quickly understand the current situation:
    who is online, what files are claimed, and the last message ID.
    This is more efficient than reading full message history.

    Returns {"ok": true, "active_agents": [...], "claims": [...], "last_msg_id": int}.
    """
    client = await _ensure_client()
    return await client._request("room_state", room=room)


def main() -> None:
    global _actor, _broker_url, _repo_root

    parser = argparse.ArgumentParser(description="A2A channel MCP shim")
    parser.add_argument("--actor", required=True, help="Actor name (claude/codex/user)")
    parser.add_argument("--broker", default="ws://127.0.0.1:9100", help="Broker WebSocket URL")
    parser.add_argument("--cwd", default="", help="Repo root for git operations (default: process cwd)")
    args = parser.parse_args()
    _actor = args.actor
    _broker_url = args.broker
    _repo_root = args.cwd
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
