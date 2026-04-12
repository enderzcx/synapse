"""End-to-end pingpong: spawn both agents as subprocesses and exercise
all five routing paths through real HTTP/JSON-RPC.

Uses sys.executable (not bare 'python') and waits on the actual
/.well-known/agent-card.json endpoint returning 200 (not just TCP connect)
— v2 codex review MEDIUM #6.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import subprocess
import sys
import time

import httpx
import pytest

from warroom.scripts.ping import send_ping

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / ".pytest_logs"

CLAUDE_PORT = 9001
CODEX_PORT = 9002


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
        f"agent {name} on :{port} not ready in {timeout}s (last error: {last_err!r})"
    )


@pytest.fixture(scope="module")
def agents():
    LOG_DIR.mkdir(exist_ok=True)
    procs: list[tuple[subprocess.Popen, object, str, int]] = []
    for module, name, port in [
        ("warroom.apps.claude", "claude", CLAUDE_PORT),
        ("warroom.apps.codex", "codex", CODEX_PORT),
    ]:
        log = open(LOG_DIR / f"{name}.log", "w", encoding="utf-8")
        p = subprocess.Popen(
            [sys.executable, "-m", module],
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        procs.append((p, log, name, port))

    try:
        for _p, _log, name, port in procs:
            _wait_ready(port, name)
        yield
    finally:
        for p, log, _name, _port in procs:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
            log.close()


# === Happy path: cross-agent relay both directions ===

async def test_claude_relays_to_codex(agents):
    text = await send_ping(f"http://127.0.0.1:{CLAUDE_PORT}/", "ping codex")
    assert "claude relayed" in text
    assert "pong from codex" in text


async def test_codex_relays_to_claude(agents):
    text = await send_ping(f"http://127.0.0.1:{CODEX_PORT}/", "ping claude")
    assert "codex relayed" in text
    assert "pong from claude" in text


# === Self target — must NOT relay ===

async def test_self_target_no_relay(agents):
    text = await send_ping(f"http://127.0.0.1:{CLAUDE_PORT}/", "ping claude")
    assert text == "pong from claude"


# === No ping prefix — must respond locally ===

async def test_no_ping_prefix_local_pong(agents):
    text = await send_ping(f"http://127.0.0.1:{CLAUDE_PORT}/", "yo")
    assert text == "pong from claude"


# === v3 unknown target — KNOWN_AGENTS first defense layer ===

async def test_unknown_target_no_relay(agents):
    """target 'zhuge' not in KNOWN_AGENTS → MUST be a local short-circuit.
    v4 codex review 2nd #2: exact equality, never substring contains —
    'claude relayed: unknown target 'zhuge' from codex' would otherwise
    sneak through and we'd ship the bug."""
    text = await asyncio.wait_for(
        send_ping(f"http://127.0.0.1:{CLAUDE_PORT}/", "ping zhuge"),
        timeout=5.0,
    )
    assert text == "unknown target 'zhuge' from claude"


# === AgentCard discovery — explicit regression ===

def test_agent_card_well_known(agents):
    r = httpx.get(
        f"http://127.0.0.1:{CLAUDE_PORT}/.well-known/agent-card.json", timeout=2.0
    )
    assert r.status_code == 200
    card = r.json()
    assert card["name"] == "claude"
    assert card["url"] == f"http://127.0.0.1:{CLAUDE_PORT}/"
    assert "skills" in card and len(card["skills"]) > 0
