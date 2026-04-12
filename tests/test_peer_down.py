"""Peer-down regression — independent test module to avoid colliding with
the module-scoped fixture in test_pingpong.py.

v5 codex review 3rd findings addressed here:
  - #1 (MED): assert exact 'peer unreachable' prefix from PeerUnreachableError,
    NOT the loose 'relay error' prefix that any unrelated bug could match
  - #2 (LOW): use socket.bind(0) to pick free ports instead of fixed 9101/9102,
    so external port occupation cannot break the test
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import socket
import subprocess
import sys
import time

import httpx
import pytest

from warroom.scripts.ping import send_ping

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / ".pytest_logs"


def _free_port() -> int:
    """Ask OS for a free port. Race window is tiny but real — acceptable for tests."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(port: int, name: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/.well-known/agent-card.json"
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code == 200 and name in r.text:
                return
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(0.2)
    raise TimeoutError(
        f"agent {name}@:{port} not ready in {timeout}s (last error: {last_err!r})"
    )


@pytest.fixture
def alt_claude_dead_peer():
    """Spawn a 'claude' agent on a free port whose peer points at another
    free port that nobody is listening on. Yields the alive port."""
    LOG_DIR.mkdir(exist_ok=True)
    alive_port = _free_port()
    dead_port = _free_port()
    while dead_port == alive_port:
        dead_port = _free_port()
    dead_peer_url = f"http://127.0.0.1:{dead_port}/"
    log = open(LOG_DIR / f"alt_claude_{alive_port}.log", "w", encoding="utf-8")
    p = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "warroom.apps._alt",
            "claude",
            str(alive_port),
            dead_peer_url,
        ],
        cwd=str(ROOT),
        stdout=log,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        _wait_ready(alive_port, "claude")
        yield alive_port
    finally:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
        log.close()


async def test_peer_down_returns_unreachable_not_hang(alt_claude_dead_peer):
    """When the peer port has no listener, forward_to_peer must raise
    PeerUnreachableError, the executor must catch it and surface a
    'claude peer unreachable' artifact within the 5s relay timeout."""
    port = alt_claude_dead_peer
    text = await asyncio.wait_for(
        send_ping(f"http://127.0.0.1:{port}/", "ping codex"),
        timeout=15.0,  # forward_to_peer self-timeout 5s + slack
    )
    # v5 fix #19: stable prefix from PeerUnreachableError, not the generic
    # 'relay error' that any unrelated forward_to_peer bug would also match.
    assert text.startswith("claude peer unreachable"), f"unexpected: {text!r}"
