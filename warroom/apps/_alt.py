"""Test-only parameterized runner.

Allows tests to launch an agent on an arbitrary port with an arbitrary
peer URL, without reaching for env vars (which v5 codex review explicitly
removed for being a self-check bypass vector). Production code MUST NOT
import this module.

Usage from a subprocess:
    python -m warroom.apps._alt <name> <port> <peer_url>
"""
from __future__ import annotations

import sys

from warroom.apps._server import run


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(
            "usage: python -m warroom.apps._alt <name> <port> <peer_url>"
        )
    name = sys.argv[1]
    port = int(sys.argv[2])
    peer_url = sys.argv[3]
    run(name=name, port=port, peer_url=peer_url)


if __name__ == "__main__":
    main()
