"""Relay helper: an A2A agent calling another A2A agent over HTTP.

This is the standard host-agent pattern in the A2A protocol — an executor
fetches the peer's AgentCard, builds a Client, and sends a message just like
any other client. We do that here so EchoRelayExecutor can stay focused on
routing logic.

PeerUnreachableError is the *only* exception executors should treat as
"the other side is down". Any other Exception escaping forward_to_peer
indicates a real bug, and the executor reports it under a different prefix
so dead-peer regression tests cannot accidentally pass on unrelated failures.
"""
from __future__ import annotations

from uuid import uuid4

import httpx

from a2a.client import A2ACardResolver
from a2a.client.client import ClientConfig
from a2a.client.client_factory import ClientFactory
from a2a.client.errors import A2AClientHTTPError, A2AClientTimeoutError
from a2a.types import Message, Part, Role, TextPart


# Connection-class failures — these mean "peer not reachable", anything else
# escaping forward_to_peer is a real bug. We include both raw httpx exceptions
# (for direct httpx calls inside a2a-sdk) and a2a-sdk's own wrappers
# (A2ACardResolver wraps httpx.RequestError into A2AClientHTTPError(503)).
PEER_UNREACHABLE_HTTPX: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.NetworkError,
)
PEER_UNREACHABLE_A2A: tuple[type[Exception], ...] = (
    A2AClientHTTPError,
    A2AClientTimeoutError,
)
PEER_UNREACHABLE_ERRORS: tuple[type[Exception], ...] = (
    PEER_UNREACHABLE_HTTPX + PEER_UNREACHABLE_A2A
)


class PeerUnreachableError(Exception):
    """Raised by forward_to_peer when the peer agent cannot be reached.

    Stable public surface — executors `except` this to surface a
    'peer unreachable' artifact and stop the relay path cleanly.
    """


async def forward_to_peer(peer_url: str, text: str, timeout: float = 5.0) -> str:
    """Send a single text message to peer_url and return its first text artifact.

    Raises PeerUnreachableError on connection failures.
    Any other exception is allowed to propagate so the executor can
    classify it as a generic relay error rather than a peer-down event.
    """
    async with httpx.AsyncClient(timeout=timeout) as http:
        try:
            resolver = A2ACardResolver(httpx_client=http, base_url=peer_url)
            card = await resolver.get_agent_card()
        except PEER_UNREACHABLE_ERRORS as e:
            raise PeerUnreachableError(
                f"cannot fetch agent card from {peer_url}: {e!r}"
            ) from e

        factory = ClientFactory(ClientConfig(streaming=False, httpx_client=http))
        client = factory.create(card)

        message = Message(
            role=Role.user,
            parts=[Part(root=TextPart(text=text))],
            message_id=uuid4().hex,
            kind="message",
        )

        try:
            async for chunk in client.send_message(message):
                # send_message yields either (Task, event) tuples or a Message.
                if isinstance(chunk, tuple):
                    task, _event = chunk
                    if task.artifacts:
                        last = task.artifacts[-1]
                        for part in last.parts:
                            inner = part.root
                            if isinstance(inner, TextPart) and inner.text:
                                return inner.text
                else:
                    # Direct Message reply (some agents skip the task envelope)
                    msg = chunk
                    if msg.parts:
                        for part in msg.parts:
                            inner = part.root
                            if isinstance(inner, TextPart) and inner.text:
                                return inner.text
        except PEER_UNREACHABLE_ERRORS as e:
            raise PeerUnreachableError(
                f"send_message to {peer_url} failed: {e!r}"
            ) from e

        return "<no artifact returned>"
