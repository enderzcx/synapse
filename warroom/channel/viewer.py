"""Terminal viewer for A2A channel messages.

Connects to the broker as actor="user", displays messages in real time,
and lets the user type to post messages.

Uses prompt_toolkit PromptSession + patch_stdout so async-printed messages
don't corrupt the input line.

Run:
    uv run python -m warroom.channel.viewer \
        --broker ws://127.0.0.1:9100 --room room1
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import textwrap
from datetime import datetime

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.patch_stdout import patch_stdout

from warroom.channel.ws_client import ChannelClient

ACTOR_COLORS = {
    "claude": "ansicyan",
    "codex": "ansimagenta",
    "user": "ansiyellow",
    "system": "ansigreen",
}

# Match ``` code blocks (with optional language tag)
# Match ``` code blocks — allow any non-newline info string (c++, objective-c, etc.)
_CODE_BLOCK_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)


def _terminal_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def _format_msg(msg: dict) -> None:
    """Print a single channel message with proper formatting.

    - Header line: [HH:MM:SS] actor:
    - Content lines indented under the header
    - Code blocks get a distinct color
    - Long lines word-wrapped to terminal width

    Defensively handles malformed payloads (missing/wrong-type fields).
    """
    # Defensive normalization (HIGH 3 fix)
    try:
        ts_raw = float(msg.get("ts", 0))
    except (TypeError, ValueError):
        ts_raw = 0.0
    try:
        ts = datetime.fromtimestamp(ts_raw).strftime("%H:%M:%S")
    except (OSError, ValueError):
        ts = "??:??:??"
    actor = str(msg.get("actor", "?"))
    content = str(msg.get("content", ""))
    color = ACTOR_COLORS.get(actor, "ansiwhite")

    # System messages
    if content.startswith("[system]"):
        color = "ansigreen"
        print_formatted_text(FormattedText([
            ("ansigreen", f"  [{ts}] "),
            ("ansigreen bold", f"{content}"),
        ]))
        return

    # Header line
    prefix = f"[{ts}] "
    indent = " " * len(prefix)
    wrap_width = max(_terminal_width() - len(indent) - 2, 40)

    print_formatted_text(FormattedText([
        ("ansigray bold", prefix),
        (f"{color} bold", f"{actor}:"),
    ]))

    # Split content into code blocks and text segments
    parts = _split_code_blocks(content)

    for is_code, lang, text in parts:
        if is_code:
            # Code block: dim border + content
            print_formatted_text(FormattedText([
                ("ansigray", f"{indent}  "),
                ("ansigray bold", f"```{lang}"),
            ]))
            for line in text.splitlines():
                print_formatted_text(FormattedText([
                    ("ansigray", f"{indent}  "),
                    ("ansiwhite", line),
                ]))
            print_formatted_text(FormattedText([
                ("ansigray", f"{indent}  "),
                ("ansigray bold", "```"),
            ]))
        else:
            # Regular text: word-wrap and indent
            for paragraph in text.split("\n"):
                paragraph = paragraph.strip()
                if not paragraph:
                    continue
                # Bullet points: keep as-is with indent
                # Detect list items: -, *, bullet, or numbered (1. 2. etc.)
                list_match = re.match(r'^(\s*(?:[-*]|\d+[.)]))\s+', paragraph)
                if list_match:
                    prefix_len = len(list_match.group(0))
                    wrapped = textwrap.fill(
                        paragraph, width=wrap_width,
                        initial_indent=f"{indent}  ",
                        subsequent_indent=f"{indent}  " + " " * prefix_len,
                    )
                else:
                    wrapped = textwrap.fill(
                        paragraph, width=wrap_width,
                        initial_indent=f"{indent}  ",
                        subsequent_indent=f"{indent}  ",
                    )
                print_formatted_text(FormattedText([("", wrapped)]))

    # Blank line after message for visual separation
    print_formatted_text(FormattedText([("", "")]))


def _split_code_blocks(content: str) -> list[tuple[bool, str, str]]:
    """Split content into (is_code, lang, text) segments.

    Returns alternating text/code segments. Text segments have is_code=False.
    """
    parts: list[tuple[bool, str, str]] = []
    last_end = 0
    for m in _CODE_BLOCK_RE.finditer(content):
        # Text before this code block
        before = content[last_end:m.start()].strip()
        if before:
            parts.append((False, "", before))
        parts.append((True, m.group(1), m.group(2).strip()))
        last_end = m.end()
    # Remaining text after last code block
    remaining = content[last_end:].strip()
    if remaining:
        parts.append((False, "", remaining))
    return parts


async def _printer(client: ChannelClient, room: str) -> None:
    """Background task: receive broadcasts and print them."""
    while True:
        try:
            msg = await client.wait_new(room, timeout_s=3600)
        except ConnectionError:
            print("\n[viewer] broker connection lost", file=sys.stderr)
            break
        if msg is None:
            continue
        # HIGH 3 fix: catch rendering errors so one bad message
        # doesn't kill the entire printer loop
        try:
            _format_msg(msg)
        except Exception as e:
            print(f"  [viewer] render error: {e}", file=sys.stderr)


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
