"""POC echo + relay executor.

Two-layer anti-recursion defense:
  1. KNOWN_AGENTS allowlist: only forward to agents we know exist.
     Stops 'ping zhuge' typos at the door — without this, both agents
     would see target != self and bounce forever.
  2. self check: target == self.name → answer locally, do not relay.

Both layers are exercised exhaustively in tests/test_decide.py.

The EchoRelayExecutor wrapper around decide() lives below; the routing
brain itself is the pure decide() function so we can branch-cover it
without spinning up a server.
"""
from __future__ import annotations

import re

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)
from a2a.utils.artifact import new_text_artifact
from a2a.utils.message import new_agent_text_message
from a2a.utils.task import new_task

from a2a_local.relay import PeerUnreachableError, forward_to_peer

# v3 first defense layer: known agent allowlist
KNOWN_AGENTS = frozenset({"claude", "codex"})

# Matches "ping <target>" with optional surrounding whitespace, case-insensitive.
PING_RE = re.compile(r"^\s*ping\s+(\S+)\s*$", re.IGNORECASE)


def decide(name: str, peer_url: str | None, text: str) -> tuple[str, str]:
    """Pure routing decision.

    Returns ("local", reply) or ("relay", payload_to_forward).
    Caller (executor) executes whichever branch this picks.
    """
    m = PING_RE.match(text or "")
    if m is None:
        return ("local", f"pong from {name}")

    target = m.group(1).lower()

    # Layer 1: unknown target → never relay, never loop
    if target not in KNOWN_AGENTS:
        return ("local", f"unknown target '{target}' from {name}")

    # Layer 2: target is ourselves → answer locally
    if target == name:
        return ("local", f"pong from {name}")

    # Need a peer URL to relay
    if not peer_url:
        return ("local", f"no peer configured for {name}")

    # All checks pass: forward original text unchanged so the peer
    # can apply the same rules and converge in 1 hop.
    return ("relay", text)


def _extract_text(context: RequestContext) -> str:
    msg = context.message
    if not msg or not msg.parts:
        return ""
    for part in msg.parts:
        inner = part.root
        if isinstance(inner, TextPart) and inner.text:
            return inner.text
    return ""


class EchoRelayExecutor(AgentExecutor):
    """A2A AgentExecutor that uses decide() for routing.

    Three exception layers when relaying:
      - PeerUnreachableError → "<name> peer unreachable: ..."  (stable prefix
        for test_peer_down regression)
      - any other Exception  → "<name> relay error: ..."        (separate
        prefix so peer-down tests cannot accidentally pass on bugs)
    """

    def __init__(self, name: str, peer_url: str | None) -> None:
        self.name = name
        self.peer_url = peer_url

    async def execute(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        task = context.current_task or new_task(context.message)
        await event_queue.enqueue_event(task)

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task.id,
                context_id=task.context_id,
                kind="status-update",
                final=False,
                status=TaskStatus(
                    state=TaskState.working,
                    message=new_agent_text_message(f"{self.name} processing"),
                ),
            )
        )

        text = _extract_text(context)
        action, payload = decide(self.name, self.peer_url, text)

        if action == "local":
            result = payload
        else:  # relay
            assert self.peer_url is not None  # decide() guarantees this branch
            try:
                peer_reply = await forward_to_peer(self.peer_url, payload)
                result = f"{self.name} relayed: {peer_reply}"
            except PeerUnreachableError as e:
                result = f"{self.name} peer unreachable: {e}"
            except Exception as e:  # noqa: BLE001 — by design, see docstring
                result = f"{self.name} relay error: {e!r}"

        await event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                task_id=task.id,
                context_id=task.context_id,
                kind="artifact-update",
                artifact=new_text_artifact(name="result", text=result),
            )
        )
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task.id,
                context_id=task.context_id,
                kind="status-update",
                final=True,
                status=TaskStatus(state=TaskState.completed),
            )
        )

    async def cancel(
        self,
        context: RequestContext,
        event_queue: EventQueue,
    ) -> None:
        raise NotImplementedError("cancel not supported in POC")
