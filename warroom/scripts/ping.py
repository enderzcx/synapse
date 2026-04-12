"""CLI ping client + the send_ping() helper that tests reuse."""
from __future__ import annotations

import argparse
import asyncio
from uuid import uuid4

import httpx

from a2a.client import A2ACardResolver
from a2a.client.client import ClientConfig
from a2a.client.client_factory import ClientFactory
from a2a.types import Message, Part, Role, TextPart


TARGETS: dict[str, str] = {
    "claude": "http://127.0.0.1:9001/",
    "codex": "http://127.0.0.1:9002/",
}


async def send_ping(target_url: str, msg: str, timeout: float = 10.0) -> str:
    """Send msg to a running A2A agent at target_url and return its first text artifact."""
    async with httpx.AsyncClient(timeout=timeout) as http:
        resolver = A2ACardResolver(httpx_client=http, base_url=target_url)
        card = await resolver.get_agent_card()
        client = ClientFactory(
            ClientConfig(streaming=False, httpx_client=http)
        ).create(card)

        message = Message(
            role=Role.user,
            parts=[Part(root=TextPart(text=msg))],
            message_id=uuid4().hex,
            kind="message",
        )

        async for chunk in client.send_message(message):
            if isinstance(chunk, tuple):
                task, _event = chunk
                if task.artifacts:
                    last = task.artifacts[-1]
                    for part in last.parts:
                        inner = part.root
                        if isinstance(inner, TextPart) and inner.text:
                            return inner.text
            else:
                msg_chunk = chunk
                if msg_chunk.parts:
                    for part in msg_chunk.parts:
                        inner = part.root
                        if isinstance(inner, TextPart) and inner.text:
                            return inner.text
        return "<no artifact>"


def main() -> None:
    ap = argparse.ArgumentParser(description="A2A local POC ping client")
    ap.add_argument(
        "--to",
        choices=list(TARGETS.keys()),
        required=True,
        help="Which agent to ping (claude or codex)",
    )
    ap.add_argument("--msg", required=True, help="Message text to send")
    args = ap.parse_args()
    print(asyncio.run(send_ping(TARGETS[args.to], args.msg)))


if __name__ == "__main__":
    main()
