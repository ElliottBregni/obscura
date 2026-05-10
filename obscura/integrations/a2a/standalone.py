"""obscura.a2a.standalone — Standalone A2A server configuration and factory.

Two APIs are provided:

1. **Static-card utilities** (original API, preserved for back-compat):
   :func:`load_static_card`, :func:`apply_server_url`, :func:`apply_peers`,
   :func:`build_runtime_card`, :func:`load_peers_from_file`, and the thin
   :class:`PeerAgent` / :class:`StandaloneA2AConfig` (file-path-based) dataclasses.

2. **Programmatic server factory** (new API):
   :class:`ServerConfig` + :func:`build_standalone_server` — build a fully
   wired :class:`~obscura.integrations.a2a.server.ObscuraA2AServer` from a
   plain dataclass, no static JSON files required.

OpenClaw compatibility
----------------------
When ``ServerConfig.openclaw_compat=True`` the factory:

* Adds a ``"bearer"`` HTTP security scheme named ``"openclaw"`` to the card.
* Injects the ``"openclaw"`` tag into every advertised skill.
* Sets ``protocolVersion`` to ``"0.3"`` (OpenClaw's expected version).
"""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from obscura.integrations.a2a.agent_card import AgentCardGenerator
from obscura.integrations.a2a.types import AgentSkill, AuthScheme
from obscura.integrations.a2a.well_known import WellKnownAgentRegistry

if TYPE_CHECKING:
    from obscura.integrations.a2a.server import ObscuraA2AServer
    from obscura.integrations.a2a.service import A2AService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Original static-card dataclasses (preserved for back-compat)
# ---------------------------------------------------------------------------


@dataclass
class PeerAgent:
    """A remote A2A peer agent."""

    name: str
    url: str
    description: str = ""
    role: str = "general"
    card_url: str = ""

    def __post_init__(self) -> None:
        if not self.card_url:
            self.card_url = f"{self.url}/.well-known/agent.json"


@dataclass
class StandaloneA2AConfig:
    """Configuration for standalone A2A card serving (file-path-based).

    .. deprecated::
        Prefer :class:`ServerConfig` + :func:`build_standalone_server` for
        programmatic server setup.
    """

    agent_card_path: Path
    peers: list[PeerAgent] = field(default_factory=list)
    server_url: str = "http://localhost:8080"
    openclaw_team: str = "default"
    openclaw_role: str = "general"


def load_static_card(path: Path) -> dict[str, Any]:
    """Read and parse the static agent card JSON from *path*.

    Raises:
        FileNotFoundError: if *path* does not exist, with a helpful message.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Agent card not found at {path}. "
            "Create it or copy .well-known/agent.json from the repo root."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def apply_server_url(card: dict[str, Any], url: str) -> dict[str, Any]:
    """Return a copy of *card* with the top-level ``url`` field set to *url*."""
    updated = copy.deepcopy(card)
    updated["url"] = url
    return updated


def apply_peers(card: dict[str, Any], peers: list[PeerAgent]) -> dict[str, Any]:
    """Return a copy of *card* with the openclaw-discovery extension peers populated.

    Finds the extension whose ``id`` is ``"openclaw-discovery"`` and sets its
    ``params.peers`` to the serialised form of *peers*.  If no such extension
    exists the card is returned unchanged.
    """
    updated = copy.deepcopy(card)
    extensions: list[dict[str, Any]] = updated.get("extensions") or []
    for ext in extensions:
        if ext.get("id") == "openclaw-discovery":
            params: dict[str, Any] = ext.setdefault("params", {})
            params["peers"] = [
                {
                    "name": p.name,
                    "url": p.url,
                    "role": p.role,
                    "cardUrl": p.card_url or f"{p.url}/.well-known/agent.json",
                }
                for p in peers
            ]
            break
    return updated


def build_runtime_card(config: StandaloneA2AConfig) -> dict[str, Any]:
    """Build the runtime agent card from *config*.

    Loads the static card, patches the server URL, and injects peer agents into
    the OpenClaw discovery extension.
    """
    card = load_static_card(config.agent_card_path)
    card = apply_server_url(card, config.server_url)
    card = apply_peers(card, config.peers)
    return card


def load_peers_from_file(path: Path) -> list[PeerAgent]:
    """Load peer agents from a JSON file.

    Expected format::

        {
          "peers": [
            {"name": "...", "url": "...", "description": "...", "role": "..."}
          ]
        }

    Returns an empty list if *path* does not exist.
    """
    if not path.exists():
        return []
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return [
        PeerAgent(
            name=entry["name"],
            url=entry["url"],
            description=entry.get("description", ""),
            role=entry.get("role", "general"),
            card_url=entry.get("card_url", ""),
        )
        for entry in data.get("peers", [])
    ]
