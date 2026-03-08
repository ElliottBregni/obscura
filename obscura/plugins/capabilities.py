"""Capability grant model and resolver for Obscura plugins.

Implements least-privilege tool visibility: agents are granted capabilities,
and only tools belonging to granted capabilities are visible.

Usage::

    from obscura.plugins.capabilities import CapabilityResolver, CapabilityGrant

    resolver = CapabilityResolver(capability_index, tool_index)
    resolver.grant("agent-1", "repo.read", granted_by="policy")
    visible_tools = resolver.resolve_tools("agent-1")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from obscura.plugins.models import CapabilitySpec, ToolContribution
from obscura.plugins.registries.capability_index import CapabilityIndex
from obscura.plugins.registries.tool_index import ToolIndex

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Grant model
# ---------------------------------------------------------------------------


@dataclass
class CapabilityGrant:
    """A record of a capability granted to a grantee."""

    capability_id: str
    grantee_type: str           # "agent" | "session" | "user"
    grantee_id: str
    granted_by: str = "default" # "policy" | "user" | "plugin_default" | "admin"
    requires_approval: bool = False
    granted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class CapabilityDenial:
    """An explicit denial of a capability for a grantee."""

    capability_id: str
    grantee_type: str
    grantee_id: str
    denied_by: str = "policy"
    reason: str = ""


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class CapabilityResolver:
    """Resolves capability grants and derives tool visibility.

    The resolver maintains a grant/denial store and uses the capability
    and tool indexes to compute what tools an agent can see.
    """

    def __init__(
        self,
        capability_index: CapabilityIndex,
        tool_index: ToolIndex,
    ) -> None:
        self._cap_index = capability_index
        self._tool_index = tool_index
        self._grants: dict[str, list[CapabilityGrant]] = {}  # grantee_id → grants
        self._denials: dict[str, list[CapabilityDenial]] = {}  # grantee_id → denials

    # -- Granting ----------------------------------------------------------

    def grant(
        self,
        grantee_id: str,
        capability_id: str,
        *,
        grantee_type: str = "agent",
        granted_by: str = "user",
    ) -> CapabilityGrant:
        """Grant a capability to a grantee."""
        cap = self._cap_index.get(capability_id)
        grant = CapabilityGrant(
            capability_id=capability_id,
            grantee_type=grantee_type,
            grantee_id=grantee_id,
            granted_by=granted_by,
            requires_approval=cap.requires_approval if cap else False,
        )
        self._grants.setdefault(grantee_id, []).append(grant)
        # Remove any conflicting denial
        if grantee_id in self._denials:
            self._denials[grantee_id] = [
                d for d in self._denials[grantee_id]
                if d.capability_id != capability_id
            ]
        logger.debug("Granted %s to %s", capability_id, grantee_id)
        return grant

    def deny(
        self,
        grantee_id: str,
        capability_id: str,
        *,
        grantee_type: str = "agent",
        denied_by: str = "policy",
        reason: str = "",
    ) -> CapabilityDenial:
        """Explicitly deny a capability for a grantee."""
        denial = CapabilityDenial(
            capability_id=capability_id,
            grantee_type=grantee_type,
            grantee_id=grantee_id,
            denied_by=denied_by,
            reason=reason,
        )
        self._denials.setdefault(grantee_id, []).append(denial)
        # Remove any conflicting grant
        if grantee_id in self._grants:
            self._grants[grantee_id] = [
                g for g in self._grants[grantee_id]
                if g.capability_id != capability_id
            ]
        logger.debug("Denied %s for %s", capability_id, grantee_id)
        return denial

    def grant_defaults(self, grantee_id: str) -> list[CapabilityGrant]:
        """Grant all capabilities that have default_grant=True."""
        grants: list[CapabilityGrant] = []
        denied_ids = {d.capability_id for d in self._denials.get(grantee_id, [])}
        for cap in self._cap_index.capabilities_with_default_grant():
            if cap.id not in denied_ids:
                grants.append(self.grant(
                    grantee_id, cap.id,
                    granted_by="plugin_default",
                ))
        return grants

    # -- Resolution --------------------------------------------------------

    def resolve_for_agent(self, agent_id: str) -> set[str]:
        """Return the set of capability IDs granted to an agent."""
        denied = {d.capability_id for d in self._denials.get(agent_id, [])}
        granted = {
            g.capability_id
            for g in self._grants.get(agent_id, [])
            if g.capability_id not in denied
        }
        return granted

    def resolve_tools(self, agent_id: str) -> list[ToolContribution]:
        """Return tool contributions visible to an agent based on grants."""
        granted = self.resolve_for_agent(agent_id)
        return self._tool_index.visible_for_capabilities(granted)

    def resolve_tool_names(self, agent_id: str) -> set[str]:
        """Return tool names visible to an agent."""
        return {t.name for t in self.resolve_tools(agent_id)}

    def is_granted(self, agent_id: str, capability_id: str) -> bool:
        """Check if a capability is granted to an agent."""
        return capability_id in self.resolve_for_agent(agent_id)

    def requires_approval(self, agent_id: str, capability_id: str) -> bool:
        """Check if a granted capability requires user approval."""
        for g in self._grants.get(agent_id, []):
            if g.capability_id == capability_id:
                return g.requires_approval
        return False

    # -- Queries -----------------------------------------------------------

    def list_grants(self, grantee_id: str) -> list[CapabilityGrant]:
        return list(self._grants.get(grantee_id, []))

    def list_denials(self, grantee_id: str) -> list[CapabilityDenial]:
        return list(self._denials.get(grantee_id, []))

    def list_all_grantees(self) -> list[str]:
        return list(set(list(self._grants.keys()) + list(self._denials.keys())))


__all__ = [
    "CapabilityGrant",
    "CapabilityDenial",
    "CapabilityResolver",
]
