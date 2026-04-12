"""Step 1 RED: test cards.py before it exists."""
from a2a.types import AgentCard

from warroom.cards import build_agent_card


def test_build_agent_card_returns_agent_card():
    card = build_agent_card("claude", 9001)
    assert isinstance(card, AgentCard)


def test_build_agent_card_url_uses_port():
    card = build_agent_card("claude", 9001)
    assert "9001" in card.url
    assert card.url.startswith("http://127.0.0.1:")


def test_build_agent_card_name_propagated():
    assert build_agent_card("claude", 9001).name == "claude"
    assert build_agent_card("codex", 9002).name == "codex"


def test_build_agent_card_has_required_fields():
    card = build_agent_card("claude", 9001)
    assert card.description
    assert card.version
    assert card.default_input_modes == ["text/plain"]
    assert card.default_output_modes == ["text/plain"]
    assert len(card.skills) >= 1


def test_build_agent_card_has_ping_skill():
    card = build_agent_card("claude", 9001)
    skill_ids = {s.id for s in card.skills}
    assert "ping" in skill_ids


def test_build_agent_card_capabilities_streaming_disabled():
    card = build_agent_card("claude", 9001)
    # POC 阶段不开 streaming，用最简同步
    assert card.capabilities.streaming is False
