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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from obscura.plugins.registries.capability_index import CapabilityIndex

if TYPE_CHECKING:
    from obscura.plugins.models import CapabilitySpec, ToolContribution
    from obscura.plugins.registries.tool_index import ToolIndex

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Grant model
# ---------------------------------------------------------------------------


def list_builtin_capabilities() -> list[str]:
    """Return a list of all built-in capability IDs."""
    try:
        list: list[CapabilitySpec] = CapabilityIndex().list_all()
        return [cap.id for cap in list]
    except Exception:
        logger.debug("suppressed exception in list_builtin_capabilities", exc_info=True)
        return [
            "shell.exec",
            "file.read",
            "file.write",
            "git.ops",
            "web.browse",
            "search.web",
            "security.scan",
        ]


@dataclass
class CapabilityGrant:
    """A record of a capability granted to a grantee."""

    capability_id: str
    grantee_type: str  # "agent" | "session" | "user"
    grantee_id: str
    granted_by: str = "default"  # "policy" | "user" | "plugin_default" | "admin"
    requires_approval: bool = False
    granted_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


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

    @property
    def capability_index(self) -> CapabilityIndex:
        """Read-only access to the underlying capability index.

        Used by the composition tool-router block (and anywhere else that
        needs to read the capability graph without touching the resolver's
        grant/denial state).
        """
        return self._cap_index

    @property
    def tool_index(self) -> ToolIndex:
        """Read-only access to the underlying tool index."""
        return self._tool_index

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
                d for d in self._denials[grantee_id] if d.capability_id != capability_id
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
                g for g in self._grants[grantee_id] if g.capability_id != capability_id
            ]
        logger.debug("Denied %s for %s", capability_id, grantee_id)
        return denial

    def grant_defaults(self, grantee_id: str) -> list[CapabilityGrant]:
        """Grant all capabilities that have default_grant=True."""
        grants: list[CapabilityGrant] = []
        denied_ids = {d.capability_id for d in self._denials.get(grantee_id, [])}
        for cap in self._cap_index.capabilities_with_default_grant():
            if cap.id not in denied_ids:
                grants.append(
                    self.grant(
                        grantee_id,
                        cap.id,
                        granted_by="plugin_default",
                    ),
                )
        return grants

    # -- Resolution --------------------------------------------------------

    def resolve_for_agent(self, agent_id: str) -> set[str]:
        """Return the set of capability IDs granted to an agent."""
        denied = {d.capability_id for d in self._denials.get(agent_id, [])}
        return {
            g.capability_id
            for g in self._grants.get(agent_id, [])
            if g.capability_id not in denied
        }

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


def build_capability_map_section(tools: list[Any]) -> str:
    """Build a markdown section mapping capability groups to their tool names.

    Takes the final list of loaded tools (with ``.capability`` populated) and
    produces a human-readable section for the system prompt so the LLM
    understands that capability IDs like ``git.ops`` are permission groups,
    not tool names.

    Only capabilities with at least one loaded tool are included.
    """
    from collections import defaultdict

    groups: dict[str, list[str]] = defaultdict(list)
    for tool in tools:
        cap = getattr(tool, "capability", "")
        if cap:
            groups[cap].append(tool.name)

    if not groups:
        return ""

    lines = [
        "## Capability Groups",
        "",
        "Capabilities in config.toml are permission groups, not tool names. "
        "Each maps to these tools:",
        "",
    ]
    for cap_id in sorted(groups):
        tool_names = ", ".join(f"`{t}`" for t in sorted(groups[cap_id]))
        lines.append(f"- **{cap_id}**: {tool_names}")
    return "\n".join(lines)


def resolve_allowed_tools_from_config() -> set[str] | None:
    """Read capability grants/denies from config.toml and resolve to tool names.

    Returns a set of allowed tool names, or ``None`` if no capability filtering
    should be applied (e.g. config missing or empty grants).

    Logic:
    1. Read ``[defaults.capabilities]`` grant and deny lists from config.toml.
    2. Discover all plugin manifests and their capability metadata.
    3. Auto-include capabilities with ``default_grant = true`` in manifests.
    4. Add explicitly granted capabilities from config.
    5. Remove tools from denied capabilities.
    6. Tools with no capability (uncategorized) are always allowed.
    """
    try:
        from obscura.core.workspace import load_workspace_config
        from obscura.plugins.loader import (
            PluginLoader,
            _load_plugin_config_flag,  # pyright: ignore[reportPrivateUsage]
            get_capability_map,
        )
        from obscura.plugins.models import PluginSpec

        config = load_workspace_config()
        caps_cfg = config.get("defaults", {}).get("capabilities", {})
        grant_ids: set[str] = set(caps_cfg.get("grant", []))
        deny_ids: set[str] = set(caps_cfg.get("deny", []))

        # Merge in capabilities from the active wizard profile (if any).
        # Profiles augment the defaults; deny still wins.
        try:
            from obscura.wizard import WizardService

            active = WizardService().resolve_active_profile()
            if active is not None:
                grant_ids.update(active.capabilities)
        except Exception:
            logger.debug("active profile resolution failed", exc_info=True)

        # Discover all plugin specs to read default_grant flags
        load_builtins = _load_plugin_config_flag("load_builtins")
        loader = PluginLoader()
        all_specs: list[PluginSpec] = []
        if load_builtins:
            all_specs.extend(loader.discover_builtins())
        all_specs.extend(loader.discover_local())
        all_specs.extend(loader.discover_user())

        # Auto-grant capabilities with default_grant=true
        for plugin_spec in all_specs:
            for cap in plugin_spec.capabilities:
                if cap.default_grant:
                    grant_ids.add(cap.id)

        if not grant_ids:
            return None  # nothing to grant → no filtering

        # Remove denied from grants
        grant_ids -= deny_ids

        # Build reverse map: capability_id → set of tool names
        cap_map = get_capability_map()  # tool_name → capability_id
        cap_to_tools: dict[str, set[str]] = {}
        for tool_name, cap_id in cap_map.items():
            cap_to_tools.setdefault(cap_id, set()).add(tool_name)

        # Expand grants to tool names
        allowed: set[str] = set()
        for cap_id in grant_ids:
            allowed.update(cap_to_tools.get(cap_id, set()))

        return allowed
    except Exception:
        logger.debug("Failed to resolve capability grants from config", exc_info=True)
        return None


__all__ = [
    "CapabilityDenial",
    "CapabilityGrant",
    "CapabilityResolver",
    "build_capability_map_section",
    "resolve_allowed_tools_from_config",
]
