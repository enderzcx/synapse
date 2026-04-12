"""Terminal viewer for A2A channel messages.

Connects to the broker as actor="user", displays messages in real time,
and lets the user type to post messages.

Uses prompt_toolkit PromptSession + patch_stdout so async-printed messages
don't corrupt the input line.

Run:
    uv run python -m a2a_local.channel.viewer \
        --broker ws://127.0.0.1:9100 --room room1
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.patch_stdout import patch_stdout

from a2a_local.channel.ws_client import ChannelClient

ACTOR_COLORS = {
    "claude": "ansicyan",
    "codex": "ansimagenta",
    "user": "ansiyellow",
    "system": "ansigreen",
}


def _format_msg(msg: dict) -> FormattedText:
    ts_raw = msg.get("ts", 0)
    ts = datetime.fromtimestamp(ts_raw).strftime("%H:%M:%S")
    actor = msg.get("actor", "?")
    content = msg.get("content", "")
    color = ACTOR_COLORS.get(actor, "ansiwhite")

    # System messages (from shim bootstrap) get special color
    if content.startswith("[system]"):
        color = "ansigreen"

    return FormattedText([
        ("ansiblack bold", f"[{ts}] "),
        (f"{color} bold", f"{actor}: "),
        ("", content),
    ])


def _print_msg(msg: dict) -> None:
    """Print a message using prompt_toolkit's patch_stdout-safe printer."""
    from prompt_toolkit import print_formatted_text
    print_formatted_text(_format_msg(msg))


async def _printer(client: ChannelClient, room: str) -> None:
    """Background task: receive broadcasts and print them."""
    while True:
        try:
            msg = await client.wait_new(room, timeout_s=3600)
        except ConnectionError:
            print("\n[viewer] broker connection lost", file=sys.stderr)
            break
        if msg is None:
            continue  # timeout, loop
        _print_msg(msg)


async def run_viewer(broker_url: str, room: str) -> None:
    client = ChannelClient(broker_url, actor="user")
    await client.connect()
    try:
        await client.join(room)
    except ConnectionError as e:
        print(f"[viewer] join failed: {e}", file=sys.stderr)
        await client.close()
        return

    print(f"[viewer] joined {room} on {broker_url}. Type messages below. Ctrl+C to exit.\n")

    printer_task = asyncio.create_task(_printer(client, room))
    session: PromptSession[str] = PromptSession()

    try:
        with patch_stdout():
            while True:
                try:
                    text = await session.prompt_async("> ")
                except (KeyboardInterrupt, EOFError):
                    break
                text = text.strip()
                if not text:
                    continue
                try:
                    await client.post(room, content=text)
                except ConnectionError:
                    print("\n[viewer] broker connection lost", file=sys.stderr)
                    break
    finally:
        printer_task.cancel()
        try:
            await printer_task
        except (asyncio.CancelledError, Exception):
            pass
        await client.close()
        print("\n[viewer] bye")


def main() -> None:
    parser = argparse.ArgumentParser(description="A2A channel viewer")
    parser.add_argument("--broker", default="ws://127.0.0.1:9100")
    parser.add_argument("--room", default="room1")
    args = parser.parse_args()
    try:
        asyncio.run(run_viewer(args.broker, args.room))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
