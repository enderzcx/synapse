"""Step 2 RED: cover all branches of decide() before EchoRelayExecutor exists.

decide() is the routing brain — pure function so it can be tested without
spinning up any server. The two-layer anti-recursion defense lives here:

  1. KNOWN_AGENTS allowlist (rejects 'ping zhuge' typos at the door)
  2. self check (target == self.name → local pong, no relay)
"""
from warroom.executors.echo_relay import KNOWN_AGENTS, decide


def test_known_agents_set_contains_claude_and_codex():
    assert KNOWN_AGENTS == frozenset({"claude", "codex"})


# === relay path (both layers pass) ===

def test_decide_relay_known_target_not_self():
    assert decide("claude", "http://x", "ping codex") == ("relay", "ping codex")


def test_decide_relay_known_target_reverse_direction():
    assert decide("codex", "http://x", "ping claude") == ("relay", "ping claude")


# === local path: target is self ===

def test_decide_local_self_target():
    assert decide("codex", "http://x", "ping codex") == ("local", "pong from codex")


# === local path: unknown target (KNOWN_AGENTS first layer) ===

def test_decide_local_unknown_target():
    # v4 codex review 2nd #2: exact equality, NOT substring contains
    assert decide("claude", "http://x", "ping zhuge") == (
        "local",
        "unknown target 'zhuge' from claude",
    )


def test_decide_unknown_target_no_peer_still_reports_unknown():
    """No peer + unknown target should still walk the unknown branch,
    not 'no peer configured'."""
    assert decide("claude", None, "ping zhuge") == (
        "local",
        "unknown target 'zhuge' from claude",
    )


# === local path: no ping prefix ===

def test_decide_local_no_ping_prefix():
    assert decide("claude", "http://x", "yo") == ("local", "pong from claude")


def test_decide_local_empty_text():
    assert decide("claude", "http://x", "") == ("local", "pong from claude")


# === local path: known target but no peer configured ===

def test_decide_local_no_peer_configured():
    assert decide("claude", None, "ping codex") == (
        "local",
        "no peer configured for claude",
    )


# === case insensitivity on target token ===

def test_decide_target_case_insensitive():
    """target token is lowercased before allowlist + self check."""
    assert decide("claude", "http://x", "PING CODEX") == ("relay", "PING CODEX")


def test_decide_target_mixed_case_self():
    assert decide("codex", "http://x", "ping CODEX") == ("local", "pong from codex")
