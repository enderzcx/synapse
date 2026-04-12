"""AgentCard factory shared by both POC agents.

Builds a v0.3.x A2A AgentCard with the minimal required fields.
The plan-author intentionally does NOT use AgentInterface here — in 0.3.x
the primary endpoint goes in the top-level `url` field, and AgentInterface
is reserved for additional/secondary transports.
"""
from a2a.types import AgentCapabilities, AgentCard, AgentSkill


def build_agent_card(name: str, port: int) -> AgentCard:
    return AgentCard(
        name=name,
        description=f"A2A local POC agent: {name}",
        version="0.0.1",
        url=f"http://127.0.0.1:{port}/",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="ping",
                name="ping",
                description="Echo or relay ping messages",
                tags=["ping", "echo"],
                examples=["ping claude", "ping codex", "hi"],
            )
        ],
    )
