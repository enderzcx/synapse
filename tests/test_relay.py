"""Step 3 RED: PeerUnreachableError type and dead-peer behavior of forward_to_peer.

These run without a server: we only need to prove
  (1) the exception class exists and is the public surface
  (2) hitting a port nobody is listening on raises PeerUnreachableError, not a hang
"""
import asyncio

import pytest

from warroom.relay import PeerUnreachableError, forward_to_peer


def test_peer_unreachable_error_is_exception():
    assert issubclass(PeerUnreachableError, Exception)


def test_peer_unreachable_error_is_public():
    """Must be importable from warroom.relay so executors can `except` it."""
    from warroom import relay

    assert hasattr(relay, "PeerUnreachableError")


@pytest.mark.asyncio
async def test_forward_to_peer_dead_url_raises_unreachable():
    """Connecting to a port nobody listens on must raise PeerUnreachableError
    within the configured timeout — never hang."""
    # 127.0.0.1:1 is reserved/unused on every machine.
    with pytest.raises(PeerUnreachableError):
        await asyncio.wait_for(
            forward_to_peer("http://127.0.0.1:1/", "ping codex", timeout=2.0),
            timeout=8.0,  # 6s slack on top of forward_to_peer's own 2s
        )
