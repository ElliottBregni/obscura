"""obscura.a2a.well_known — Well-known agent registry for A2A peer discovery.

Maintains a registry of named remote agents by URL so that Obscura can
discover their ``AgentCard``s concurrently and reference them by name.

Usage::

    from obscura.integrations.a2a.well_known import DEFAULT_REGISTRY, WellKnownAgent

    # Use the pre-populated default registry
    card = await DEFAULT_REGISTRY.discover_all(client)

    # Or build from config
    registry = WellKnownAgentRegistry.from_config(config)
    agent = registry.get("openclaw")

``WellKnownAgent`` fields:
    name:        Stable short name (e.g. ``"openclaw"``).
    url:         Base URL of the remote agent.
    description: Human-readable description.
    auth_token:  Optional bearer token.
    bridge_only: When ``True`` the agent does not implement A2A natively.
                 ``discover_all()`` will skip it (logs at DEBUG instead of
                 attempting the fetch).  Use for OpenAI-compat-only peers
                 such as OpenClaw that are bridged by Obscura.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obscura.integrations.a2a.client import A2AClient
    from obscura.integrations.a2a.types import AgentCard

logger = logging.getLogger(__name__)


@dataclass
class WellKnownAgent:
    """A named remote A2A agent with a known base URL.

    Parameters
    ----------
    name:
        Stable short name used to look up the agent (e.g. ``"openclaw"``).
    url:
        Base URL of the remote A2A server (e.g. ``"http://localhost:18789"``).
    description:
        Human-readable description.
    auth_token:
        Optional bearer token used when connecting to this agent.
    bridge_only:
        When ``True`` the agent does not implement A2A natively and
        ``discover_all()`` will skip the A2A card fetch for it (logging at
        DEBUG level instead).  Intended for OpenAI-compat-only peers (e.g.
        OpenClaw) that are bridged into the A2A network by Obscura via a
        synthetic card.

    """

    name: str
    url: str
    description: str = ""
    auth_token: str | None = None
    bridge_only: bool = False


class WellKnownAgentRegistry:
    """Registry of named well-known A2A peers.

    Agents are keyed by their ``name`` field. The registry is mutable at
    runtime — call :meth:`register` to add agents after construction.
    """

    def __init__(self) -> None:
        self._agents: dict[str, WellKnownAgent] = {}

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def register(self, agent: WellKnownAgent) -> None:
        """Add *agent* to the registry (overwrites any existing entry with the same name)."""
        self._agents[agent.name] = agent

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> WellKnownAgent | None:
        """Return the agent registered under *name*, or ``None``."""
        return self._agents.get(name)

    def list(self) -> list[WellKnownAgent]:
        """Return all registered agents (insertion order preserved)."""
        return list(self._agents.values())

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def discover_all(
        self,
        client: A2AClient,
    ) -> dict[str, AgentCard]:
        """Concurrently fetch ``AgentCard``s from all registered agents.

        Uses a fresh :class:`~obscura.integrations.a2a.client.A2AClient`
        per agent so that per-agent ``auth_token`` values are respected.
        Agents that fail to respond are logged at WARNING and omitted from
        the result — discovery is best-effort.

        Parameters
        ----------
        client:
            An *already-connected* client used only as a template for
            transport configuration (timeout, etc.). Per-agent clients
            are constructed from each agent's ``url`` and ``auth_token``.

        Returns
        -------
        dict[str, AgentCard]
            Mapping of agent name → card for every agent that responded.

        """
        from obscura.integrations.a2a.client import A2AClient as _A2AClient

        async def _fetch(agent: WellKnownAgent) -> tuple[str, AgentCard | None]:
            if agent.bridge_only:
                logger.debug(
                    "Skipping A2A discovery for bridge-only agent %r at %s "
                    "(no A2A card — use synthetic card instead)",
                    agent.name,
                    agent.url,
                )
                return agent.name, None

            peer: _A2AClient = _A2AClient(
                agent.url,
                auth_token=agent.auth_token,
                timeout=getattr(client, "_timeout", 30.0),
            )
            try:
                async with peer:
                    card = await peer.discover()
                    return agent.name, card
            except Exception as exc:
                logger.warning(
                    "Failed to discover agent card for %r at %s: %s",
                    agent.name,
                    agent.url,
                    exc,
                )
                return agent.name, None

        results = await asyncio.gather(*[_fetch(a) for a in self._agents.values()])
        return {name: card for name, card in results if card is not None}

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: dict[str, object]) -> WellKnownAgentRegistry:
        """Build a registry from a config dict.

        The dict is expected to have a ``"well_known_agents"`` key whose
        value is a list of mappings with at minimum ``name`` and ``url``.
        Optional keys: ``description``, ``auth_token``.

        Example::

            config = {
                "well_known_agents": [
                    {"name": "openclaw", "url": "http://localhost:7477"},
                    {"name": "peer", "url": "http://peer.example.com", "auth_token": "s3cr3t"},
                ]
            }
            registry = WellKnownAgentRegistry.from_config(config)

        """
        registry = cls()
        raw = config.get("well_known_agents", [])
        if not isinstance(raw, list):
            logger.warning(
                "well_known_agents must be a list; got %r — skipping", type(raw)
            )
            return registry

        for entry in raw:
            if not isinstance(entry, dict):
                logger.warning(
                    "Skipping non-dict entry in well_known_agents: %r", entry
                )
                continue
            name = str(entry.get("name", "")).strip()
            url = str(entry.get("url", "")).strip()
            if not name or not url:
                logger.warning(
                    "Skipping well_known_agents entry missing name/url: %r",
                    entry,
                )
                continue
            registry.register(
                WellKnownAgent(
                    name=name,
                    url=url,
                    description=str(entry.get("description", "")),
                    auth_token=entry.get("auth_token") or None,  # type: ignore[arg-type]
                )
            )

        return registry


# ---------------------------------------------------------------------------
# Module-level default registry
# ---------------------------------------------------------------------------

DEFAULT_REGISTRY: WellKnownAgentRegistry = WellKnownAgentRegistry()

DEFAULT_REGISTRY.register(
    WellKnownAgent(
        name="openclaw",
        # OpenClaw speaks OpenAI-compat only (POST /v1/chat/completions).
        # It has NO A2A server — /.well-known/agent.json always 404s.
        # Obscura bridges it via OpenClawBridge and advertises a synthetic card.
        # See: obscura/integrations/a2a/openclaw_bridge.py::openclaw_synthetic_card
        url="http://localhost:18789",
        description="OpenClaw agent runtime (Molty) — chat completions gateway",
        bridge_only=True,
    )
)

DEFAULT_REGISTRY.register(
    WellKnownAgent(
        name="obscura_local",
        url="http://localhost:8080",
        description="Local Obscura A2A endpoint",
    )
)


__all__ = [
    "DEFAULT_REGISTRY",
    "WellKnownAgent",
    "WellKnownAgentRegistry",
]
