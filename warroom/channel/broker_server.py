"""WebSocket server entrypoint for the channel broker.

Wraps the transport-agnostic `Broker` with a real `websockets.serve` handler.
Each client connection gets its own `ConnState`; frames are decoded per
message and handed to `Broker.handle_frame`.

Run standalone:
    uv run python -m warroom.channel.broker_server \
        --host 127.0.0.1 --port 9100 --db ~/.a2a_channel.db
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import uuid
from pathlib import Path

import websockets

from warroom.channel.broker import Broker, ConnState
from warroom.channel.db import init_db

logger = logging.getLogger("a2a.channel.broker")


async def _handle(ws: websockets.WebSocketServerProtocol, broker: Broker) -> None:
    """Per-connection handler."""
    state = ConnState(ws=ws, client_id="pending-" + uuid.uuid4().hex[:8])
    try:
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError as e:
                logger.warning("invalid json from %s: %s", state.client_id, e)
                try:
                    await ws.send(json.dumps({
                        "op": "error",
                        "code": "bad_json",
                        "message": str(e),
                    }))
                except Exception:
                    break
                continue
            if not isinstance(frame, dict):
                logger.warning("non-dict frame from %s", state.client_id)
                continue
            await broker.handle_frame(state, frame)
    except websockets.ConnectionClosed:
        pass
    except Exception as e:  # noqa: BLE001
        logger.exception("broker handler crashed: %s", e)
    finally:
        await broker.on_disconnect(state)


async def serve(
    host: str = "127.0.0.1",
    port: int = 9100,
    db_path: str | Path = ":memory:",
    stop_event: asyncio.Event | None = None,
    ready_event: asyncio.Event | None = None,
    bound_port_box: list[int] | None = None,
) -> None:
    """Start the broker server; blocks until stop_event is set (or forever).

    v5 LOW 4 fix: if `port=0`, bind an OS-assigned free port and report
    the real port via `bound_port_box` (a 1-element list used as out-param
    to avoid refactoring callers). Tests use this to eliminate the
    bind→close→server-start TOCTOU race.
    """
    db = init_db(str(db_path))
    broker = Broker(db=db)

    async def handler(ws):
        await _handle(ws, broker)

    async with websockets.serve(handler, host, port) as server:
        # Discover the real bound port (important when port=0)
        real_port = port
        for sock in server.sockets or []:
            try:
                real_port = sock.getsockname()[1]
                break
            except Exception:
                continue
        if bound_port_box is not None:
            bound_port_box.append(real_port)
        if ready_event is not None:
            ready_event.set()
        logger.info("broker serving on ws://%s:%d (db=%s)", host, real_port, db_path)

        if stop_event is None:
            await asyncio.Future()  # run forever
        else:
            await stop_event.wait()
    db.close()
    logger.info("broker stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="A2A channel broker server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument(
        "--db",
        default=str(Path.home() / ".a2a_channel.db"),
        help="SQLite db path; ':memory:' for ephemeral",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    stop_event = asyncio.Event()

    def _on_signal(*_: object) -> None:
        stop_event.set()

    # Windows: signal handlers inside asyncio loop are limited; use signal.signal for SIGINT
    try:
        signal.signal(signal.SIGINT, _on_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _on_signal)
    except (ValueError, OSError):
        pass

    asyncio.run(serve(
        host=args.host,
        port=args.port,
        db_path=args.db,
        stop_event=stop_event,
    ))


if __name__ == "__main__":
    main()
