"""
obscura.a2a.agent_card — Generate A2A Agent Cards from Obscura config.

Reads ``AgentConfig`` and ``ObscuraConfig`` to produce a well-formed
``AgentCard`` suitable for publication at ``/.well-known/agent.json``.

Maps:
    config.name      → card.name
    config.tools     → card.skills  (one skill per tool)
    auth config      → card.securitySchemes
"""

from __future__ import annotations

from typing import Any

from obscura.integrations.a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    AuthScheme,
)


class AgentCardGenerator:
    """Builds an ``AgentCard`` from Obscura configuration objects.

    Parameters
    ----------
    name:
        Agent display name.
    url:
        Base URL where the A2A server is hosted (e.g. ``https://api.example.com``).
    description:
        Human-readable description of the agent.
    version:
        Agent version string (defaults to ``"1.0"``).
    """

    def __init__(
        self,
        name: str,
        url: str,
        *,
        description: str = "",
        version: str = "1.0",
    ) -> None:
        self._name = name
        self._url = url
        self._description = description
        self._version = version
        self._skills: list[AgentSkill] = []
        self._capabilities = AgentCapabilities()
        self._security_schemes: dict[str, AuthScheme] = {}
        self._security: list[dict[str, list[str]]] = []
        self._provider: dict[str, str] | None = None

    # ----- Builder methods -----

    def with_skills(self, skills: list[AgentSkill]) -> AgentCardGenerator:
        """Set skills directly."""
        self._skills = skills
        return self

    def with_skills_from_tools(
        self,
        tools: list[dict[str, Any]],
    ) -> AgentCardGenerator:
        """Generate skills from a list of tool-spec-like dicts.

        Each dict should have ``name``, ``description``, and optionally
        ``tags`` / ``required_tier``.
        """
        for t in tools:
            self._skills.append(
                AgentSkill(
                    id=t.get("name", ""),
                    name=t.get("name", ""),
                    description=t.get("description", ""),
                    tags=t.get("tags", []),
                )
            )
        return self

    def with_capabilities(
        self,
        *,
        streaming: bool = True,
        push_notifications: bool = False,
        extended_card: bool = False,
    ) -> AgentCardGenerator:
        """Set agent capabilities."""
        self._capabilities = AgentCapabilities(
            streaming=streaming,
            pushNotifications=push_notifications,
            extendedAgentCard=extended_card,
        )
        return self

    def with_bearer_auth(self) -> AgentCardGenerator:
        """Add HTTP Bearer authentication scheme."""
        self._security_schemes["bearer"] = AuthScheme(
            type="http",
            scheme="bearer",
        )
        self._security = [{"bearer": []}]
        return self

    def with_auth_scheme(
        self,
        name: str,
        scheme: AuthScheme,
    ) -> AgentCardGenerator:
        """Add a custom authentication scheme."""
        self._security_schemes[name] = scheme
        if not any(name in entry for entry in self._security):
            self._security.append({name: []})
        return self

    def with_provider(
        self,
        name: str,
        url: str = "",
    ) -> AgentCardGenerator:
        """Set the agent provider information."""
        self._provider = {"name": name}
        if url:
            self._provider["url"] = url
        return self

    # ----- Build -----

    def build(self) -> AgentCard:
        """Generate the final AgentCard."""
        return AgentCard(
            name=self._name,
            description=self._description,
            url=self._url,
            version=self._version,
            skills=self._skills,
            capabilities=self._capabilities,
            securitySchemes=self._security_schemes,
            security=self._security,
            provider=self._provider,
        )

    # ----- Convenience class method -----

    @classmethod
    def from_agent_config(
        cls,
        agent_name: str,
        base_url: str,
        *,
        description: str = "",
        tools: list[dict[str, Any]] | None = None,
        streaming: bool = True,
        auth_enabled: bool = True,
        provider_name: str = "Obscura",
        provider_url: str = "https://obscura.dev",
    ) -> AgentCard:
        """One-shot: build an AgentCard from common Obscura agent params.

        Parameters
        ----------
        agent_name:
            Agent display name (maps to ``AgentConfig.name``).
        base_url:
            Server URL where A2A is hosted.
        description:
            Agent description.
        tools:
            List of tool-spec dicts (each with ``name``, ``description``).
        streaming:
            Whether the agent supports streaming.
        auth_enabled:
            Whether to declare bearer auth in the card.
        provider_name:
            Name of the provider organization.
        provider_url:
            URL of the provider organization.
        """
        builder = cls(
            name=agent_name,
            url=base_url,
            description=description,
        )

        if tools:
            builder.with_skills_from_tools(tools)

        builder.with_capabilities(streaming=streaming)

        if auth_enabled:
            builder.with_bearer_auth()

        builder.with_provider(provider_name, provider_url)

        return builder.build()
