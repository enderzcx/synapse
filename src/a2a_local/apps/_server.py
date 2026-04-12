"""Shared server bootstrap for both POC agents.

Hard guards (POC v5):
  1. name must be in KNOWN_AGENTS — prevents typos producing un-routable agents.
  2. peer_url must not point at self — prevents self-pointing typos that
     would create infinite self-relay loops on `ping <other>`.

We do NOT read env vars here. v5 codex review 2nd #3 found that allowing
A2A_NAME / A2A_PEER_URL overrides reintroduces relay-loop fragility.
For the POC, the wrapper modules (claude.py, codex.py) hardcode constants;
tests use _alt.py to parameterize.
"""
from __future__ import annotations

import uvicorn

from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore

from a2a_local.cards import build_agent_card
from a2a_local.executors.echo_relay import KNOWN_AGENTS, EchoRelayExecutor


def _self_url_variants(port: int) -> set[str]:
    return {
        f"http://127.0.0.1:{port}/",
        f"http://127.0.0.1:{port}",
        f"http://localhost:{port}/",
        f"http://localhost:{port}",
    }


def run(name: str, port: int, peer_url: str) -> None:
    # Hard guard 1: known name (defense in depth — wrappers already pass valid)
    if name not in KNOWN_AGENTS:
        raise SystemExit(
            f"invalid agent name {name!r}, must be one of {sorted(KNOWN_AGENTS)}"
        )

    # Hard guard 2: peer_url must not point to self
    normalized_peer = peer_url.rstrip("/") + "/"
    self_normalized = {u.rstrip("/") + "/" for u in _self_url_variants(port)}
    if normalized_peer in self_normalized:
        raise SystemExit(
            f"peer_url {peer_url!r} points to self (port {port}), "
            "would create infinite self-relay loop"
        )

    handler = DefaultRequestHandler(
        agent_executor=EchoRelayExecutor(name=name, peer_url=peer_url),
        task_store=InMemoryTaskStore(),
    )
    server = A2AStarletteApplication(
        agent_card=build_agent_card(name, port),
        http_handler=handler,
    )
    uvicorn.run(server.build(), host="127.0.0.1", port=port, log_level="warning")
