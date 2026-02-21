"""Tests for sdk.a2a.agent_card — AgentCard generation from config."""

from __future__ import annotations

from sdk.a2a.agent_card import AgentCardGenerator
from sdk.a2a.types import AgentCard, AgentSkill, AuthScheme


class TestAgentCardGenerator:
    def test_minimal_build(self) -> None:
        card = AgentCardGenerator("TestAgent", "https://example.com/a2a").build()
        assert card.name == "TestAgent"
        assert card.url == "https://example.com/a2a"
        assert card.version == "1.0"
        assert card.protocolVersion == "0.3"
        assert card.skills == []
        assert card.capabilities.streaming is True

    def test_with_description(self) -> None:
        card = AgentCardGenerator(
            "Bot", "https://bot.dev", description="A helpful bot"
        ).build()
        assert card.description == "A helpful bot"

    def test_with_skills(self) -> None:
        skills = [
            AgentSkill(id="triage", name="Triage", description="Classify tickets"),
            AgentSkill(id="resolve", name="Resolve", tags=["billing"]),
        ]
        card = AgentCardGenerator("Agent", "https://x.com").with_skills(skills).build()
        assert len(card.skills) == 2
        assert card.skills[0].id == "triage"
        assert card.skills[1].tags == ["billing"]

    def test_with_skills_from_tools(self) -> None:
        tools = [
            {"name": "search", "description": "Search documents"},
            {"name": "deploy", "description": "Deploy to staging", "tags": ["infra"]},
        ]
        card = AgentCardGenerator("Agent", "https://x.com").with_skills_from_tools(tools).build()
        assert len(card.skills) == 2
        assert card.skills[0].id == "search"
        assert card.skills[0].name == "search"
        assert card.skills[1].tags == ["infra"]

    def test_with_capabilities(self) -> None:
        card = (
            AgentCardGenerator("Agent", "https://x.com")
            .with_capabilities(streaming=False, push_notifications=True, extended_card=True)
            .build()
        )
        assert card.capabilities.streaming is False
        assert card.capabilities.pushNotifications is True
        assert card.capabilities.extendedAgentCard is True

    def test_with_bearer_auth(self) -> None:
        card = AgentCardGenerator("Agent", "https://x.com").with_bearer_auth().build()
        assert "bearer" in card.securitySchemes
        assert card.securitySchemes["bearer"].type == "http"
        assert card.securitySchemes["bearer"].scheme == "bearer"
        assert card.security == [{"bearer": []}]

    def test_with_custom_auth_scheme(self) -> None:
        card = (
            AgentCardGenerator("Agent", "https://x.com")
            .with_auth_scheme("apiKey", AuthScheme(type="apiKey", name="X-API-Key", **{"in": "header"}))
            .build()
        )
        assert "apiKey" in card.securitySchemes
        assert card.security == [{"apiKey": []}]

    def test_with_provider(self) -> None:
        card = (
            AgentCardGenerator("Agent", "https://x.com")
            .with_provider("Obscura", "https://obscura.dev")
            .build()
        )
        assert card.provider is not None
        assert card.provider["name"] == "Obscura"
        assert card.provider["url"] == "https://obscura.dev"

    def test_provider_name_only(self) -> None:
        card = AgentCardGenerator("Agent", "https://x.com").with_provider("Acme").build()
        assert card.provider == {"name": "Acme"}

    def test_chaining(self) -> None:
        """All builder methods return self for chaining."""
        card = (
            AgentCardGenerator("Agent", "https://x.com")
            .with_skills([AgentSkill(id="s1", name="Skill")])
            .with_capabilities(streaming=True)
            .with_bearer_auth()
            .with_provider("Obscura", "https://obscura.dev")
            .build()
        )
        assert card.name == "Agent"
        assert len(card.skills) == 1
        assert "bearer" in card.securitySchemes
        assert card.provider is not None


class TestFromAgentConfig:
    def test_basic(self) -> None:
        card = AgentCardGenerator.from_agent_config(
            agent_name="Support Agent",
            base_url="https://api.example.com",
            description="Handles customer support",
        )
        assert isinstance(card, AgentCard)
        assert card.name == "Support Agent"
        assert card.url == "https://api.example.com"
        assert card.description == "Handles customer support"

    def test_with_tools(self) -> None:
        card = AgentCardGenerator.from_agent_config(
            agent_name="Tooled Agent",
            base_url="https://api.example.com",
            tools=[
                {"name": "search_tickets", "description": "Search support tickets"},
                {"name": "query_customer", "description": "Look up customer info"},
            ],
        )
        assert len(card.skills) == 2
        assert card.skills[0].id == "search_tickets"

    def test_auth_enabled(self) -> None:
        card = AgentCardGenerator.from_agent_config(
            agent_name="Agent",
            base_url="https://x.com",
            auth_enabled=True,
        )
        assert "bearer" in card.securitySchemes
        assert card.security == [{"bearer": []}]

    def test_auth_disabled(self) -> None:
        card = AgentCardGenerator.from_agent_config(
            agent_name="Agent",
            base_url="https://x.com",
            auth_enabled=False,
        )
        assert card.securitySchemes == {}
        assert card.security == []

    def test_streaming_flag(self) -> None:
        card = AgentCardGenerator.from_agent_config(
            agent_name="Agent",
            base_url="https://x.com",
            streaming=False,
        )
        assert card.capabilities.streaming is False

    def test_provider_info(self) -> None:
        card = AgentCardGenerator.from_agent_config(
            agent_name="Agent",
            base_url="https://x.com",
            provider_name="AcmeCorp",
            provider_url="https://acme.com",
        )
        assert card.provider is not None
        assert card.provider["name"] == "AcmeCorp"
        assert card.provider["url"] == "https://acme.com"

    def test_default_provider(self) -> None:
        card = AgentCardGenerator.from_agent_config(
            agent_name="Agent",
            base_url="https://x.com",
        )
        assert card.provider is not None
        assert card.provider["name"] == "Obscura"


class TestCardSerialization:
    def test_generated_card_serializes(self) -> None:
        card = (
            AgentCardGenerator("Agent", "https://x.com")
            .with_skills([AgentSkill(id="s1", name="Search")])
            .with_bearer_auth()
            .with_provider("Obscura")
            .build()
        )
        json_str = card.model_dump_json()
        restored = AgentCard.model_validate_json(json_str)
        assert restored.name == "Agent"
        assert len(restored.skills) == 1
        assert "bearer" in restored.securitySchemes
