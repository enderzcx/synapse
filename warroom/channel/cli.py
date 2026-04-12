"""warroom start — one command to launch broker + viewer.

Usage:
    uv run python -m warroom.channel.cli start
    uv run python -m warroom.channel.cli start --no-viewer
    uv run python -m warroom.channel.cli stop
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from warroom.channel.broker_server import serve as serve_broker


async def _start(host: str, port: int, db_path: str, room: str, no_viewer: bool) -> None:
    stop = asyncio.Event()

    def _on_signal(*_: object) -> None:
        stop.set()

    try:
        signal.signal(signal.SIGINT, _on_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _on_signal)
    except (ValueError, OSError):
        pass

    # Start broker
    ready = asyncio.Event()
    bound: list[int] = []
    broker_task = asyncio.create_task(serve_broker(
        host=host, port=port, db_path=db_path,
        stop_event=stop, ready_event=ready, bound_port_box=bound,
    ))

    try:
        await asyncio.wait_for(ready.wait(), timeout=5.0)
    except asyncio.TimeoutError:
        print("[warroom] broker failed to start", file=sys.stderr)
        stop.set()
        await broker_task
        return

    real_port = bound[0] if bound else port
    broker_url = f"ws://{host}:{real_port}"
    print(f"[warroom] broker ready on {broker_url}")

    if no_viewer:
        print(f"[warroom] waiting for agents to connect... (Ctrl+C to stop)")
        await stop.wait()
    else:
        # Run viewer inline (same process)
        from warroom.channel.viewer import run_viewer
        print(f"[warroom] starting viewer for {room}...\n")
        viewer_task = asyncio.create_task(run_viewer(broker_url, room))

        # Wait for either viewer exit or stop signal
        done, pending = await asyncio.wait(
            [viewer_task, asyncio.create_task(stop.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        stop.set()
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    # Shutdown broker
    stop.set()
    try:
        await asyncio.wait_for(broker_task, timeout=3.0)
    except asyncio.TimeoutError:
        broker_task.cancel()
        try:
            await broker_task
        except (asyncio.CancelledError, Exception):
            pass

    print("[warroom] stopped")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="warroom",
        description="Warroom — let AI coding agents talk to each other",
    )
    sub = parser.add_subparsers(dest="command")

    start_p = sub.add_parser("start", help="Start broker + viewer")
    start_p.add_argument("--host", default="127.0.0.1")
    start_p.add_argument("--port", type=int, default=9100)
    start_p.add_argument("--db", default=os.path.join(os.path.expanduser("~"), ".warroom.db"))
    start_p.add_argument("--room", default="room1")
    start_p.add_argument("--no-viewer", action="store_true", help="Start broker only (headless)")

    args = parser.parse_args()

    if args.command == "start":
        try:
            asyncio.run(_start(args.host, args.port, args.db, args.room, args.no_viewer))
        except KeyboardInterrupt:
            pass
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
