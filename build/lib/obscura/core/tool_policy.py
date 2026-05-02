"""Unified tool restriction policy across all backends.

Provides a backend-agnostic way to control which tools are available to models,
including the ability to restrict native backend tools and manage custom tool access.

Example usage::

    from obscura.core.tool_policy import ToolPolicy

    # Only allow registered custom tools (block native tools)
    policy = ToolPolicy.custom_only()

    # Allow everything (custom + native)
    policy = ToolPolicy.allow_all()

    # Custom restrictions
    policy = ToolPolicy(
        allow_native=False,
        allowed_tools=["search", "file_read"],
        denied_tools=["file_write"]
    )

    # Action-level restrictions on unified tools
    policy = ToolPolicy(
        allowed_tools=["git"],
        allowed_actions={"git": frozenset({"status", "diff", "log"})},
    )

    # From environment variable
    policy = ToolPolicy.from_env()
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, override

if TYPE_CHECKING:
    from obscura.core.types import ToolSpec


def _empty_action_map() -> dict[str, frozenset[str]]:
    return {}


@dataclass(frozen=True)
class ToolPolicy:
    """Controls which tools are available to the model across all backends.

    This policy provides unified tool access control that works consistently
    across Copilot, Claude, OpenAI, and other backends.

    Attributes:
        allow_native: If False, blocks backend's built-in/native tools (default: False)
        allowed_tools: Whitelist of specific tool names (None = allow all registered)
        denied_tools: Blacklist of specific tool names to block
        allowed_actions: Per-tool whitelist of allowed ``action`` parameter values.
            When a tool appears in this map, only the listed actions are permitted.
            Tools not in the map are unrestricted.
        denied_actions: Per-tool blacklist of denied ``action`` parameter values.
            When a tool appears in this map, the listed actions are blocked.

    The policy is applied in this order:
    1. If allow_native=False, native backend tools are blocked
    2. If allowed_tools is set, only those tools are available
    3. If denied_tools is set, those tools are removed from the allowed set
    4. If allowed_actions/denied_actions is set, action parameter is checked

    Examples::

        # Only custom tools (most common)
        policy = ToolPolicy.custom_only()

        # Allow native + custom
        policy = ToolPolicy.allow_all()

        # Only specific tools
        policy = ToolPolicy(
            allow_native=False,
            allowed_tools=["search", "fetch", "read_file"]
        )

        # Block specific tools
        policy = ToolPolicy(
            allow_native=True,
            denied_tools=["execute_code", "shell_exec"]
        )

        # Allow git but only read actions
        policy = ToolPolicy(
            allowed_tools=["git"],
            allowed_actions={"git": frozenset({"status", "diff", "log"})},
        )

    """

    allow_native: bool = False
    allowed_tools: list[str] | None = None
    denied_tools: list[str] | None = None
    allowed_actions: dict[str, frozenset[str]] = field(
        default_factory=_empty_action_map,
    )
    denied_actions: dict[str, frozenset[str]] = field(
        default_factory=_empty_action_map,
    )

    # -- Factory methods ----------------------------------------------------

    @classmethod
    def custom_only(cls) -> ToolPolicy:
        """Create a policy that only allows registered custom tools.

        Blocks all native backend tools (Copilot's built-ins, Claude's native tools, etc.)
        This is the recommended default for most use cases.

        Returns:
            ToolPolicy with allow_native=False

        """
        return cls(allow_native=False)

    @classmethod
    def allow_all(cls) -> ToolPolicy:
        """Create a policy that allows both custom and native tools.

        Useful when you want the model to have access to all capabilities,
        including the backend's built-in tools.

        Returns:
            ToolPolicy with allow_native=True

        """
        return cls(allow_native=True)

    @classmethod
    def restricted(cls, allowed: list[str]) -> ToolPolicy:
        """Create a policy that only allows specific tools by name.

        Args:
            allowed: List of tool names to allow

        Returns:
            ToolPolicy with specific allowed_tools

        Example::

            policy = ToolPolicy.restricted(["search", "fetch", "read_file"])

        """
        return cls(allow_native=False, allowed_tools=allowed)

    @classmethod
    def blocked(cls, denied: list[str]) -> ToolPolicy:
        """Create a policy that blocks specific tools by name.

        All other tools (custom and native) are allowed.

        Args:
            denied: List of tool names to block

        Returns:
            ToolPolicy with specific denied_tools

        Example::

            policy = ToolPolicy.blocked(["execute_code", "shell_exec"])

        """
        return cls(allow_native=True, denied_tools=denied)

    @classmethod
    def from_env(cls, var_name: str = "OBSCURA_ALLOW_NATIVE_TOOLS") -> ToolPolicy:
        """Create policy from environment variable.

        Args:
            var_name: Environment variable name (default: OBSCURA_ALLOW_NATIVE_TOOLS)

        Returns:
            ToolPolicy based on environment configuration

        The environment variable should be set to:
        - "true", "1", or "yes" to allow native tools
        - "false", "0", or "no" to block native tools (default)

        Example::

            export OBSCURA_ALLOW_NATIVE_TOOLS=false
            policy = ToolPolicy.from_env()  # allow_native=False

        """
        value = os.environ.get(var_name, "false").lower()
        allow_native = value in ("true", "1", "yes")
        return cls(allow_native=allow_native)

    @classmethod
    def subagent_only(cls) -> ToolPolicy:
        """Create a policy for sub-agents: only run_shell is permitted.

        All native Claude / Copilot / OpenAI tools are blocked.  The only
        custom tool allowed is ``run_shell`` (bash execution).  This is the
        policy automatically applied by ``inject_subagent_context()`` when a
        child agent is spawned via the Task-tool delegation system.

        Returns:
            ToolPolicy with allow_native=False and allowed_tools=["run_shell"]

        Example::

            policy = ToolPolicy.subagent_only()
            # allow_native=False, allowed_tools=["run_shell"]

        """
        return cls(allow_native=False, allowed_tools=["run_shell"])

    # -- Utility methods ----------------------------------------------------

    def is_tool_allowed(
        self,
        tool_name: str,
        is_native: bool = False,
        action: str | None = None,
    ) -> bool:
        """Check if a specific tool is allowed by this policy.

        Args:
            tool_name: Name of the tool to check
            is_native: Whether this is a native backend tool
            action: Optional action parameter value (for unified tools with
                an ``action`` enum like ``git``).  When provided, the call is
                also checked against ``allowed_actions`` / ``denied_actions``.

        Returns:
            True if the tool is allowed, False otherwise

        Example::

            policy = ToolPolicy.custom_only()
            policy.is_tool_allowed("search", is_native=False)  # True
            policy.is_tool_allowed("native_tool", is_native=True)  # False

            # Action-level check
            policy = ToolPolicy(
                allowed_actions={"git": frozenset({"status", "diff", "log"})},
            )
            policy.is_tool_allowed("git", action="status")  # True
            policy.is_tool_allowed("git", action="push")    # False

        """
        # Check native tool restriction
        if is_native and not self.allow_native:
            return False

        # Check denied list first
        if self.denied_tools and tool_name in self.denied_tools:
            return False

        # Check allowed list
        if self.allowed_tools and tool_name not in self.allowed_tools:
            return False

        # Check action-level restrictions
        if action is not None:
            if tool_name in self.denied_actions:
                if action in self.denied_actions[tool_name]:
                    return False
            if tool_name in self.allowed_actions:
                if action not in self.allowed_actions[tool_name]:
                    return False

        return True

    def filter_tools(
        self,
        tools: list[ToolSpec],
        include_native: bool = False,
    ) -> list[ToolSpec]:
        """Filter a list of tools according to this policy.

        Args:
            tools: List of tools to filter
            include_native: Whether to check native tool restrictions

        Returns:
            Filtered list of tools

        Example::

            policy = ToolPolicy.restricted(["search", "fetch"])
            available = policy.filter_tools(all_tools)
            # available only contains search and fetch tools

        """
        if not self.allow_native and include_native:
            # If we're blocking native and caller says these might be native,
            # filter them all out
            return []

        filtered = tools

        if self.allowed_tools:
            filtered = [t for t in filtered if t.name in self.allowed_tools]

        if self.denied_tools:
            filtered = [t for t in filtered if t.name not in self.denied_tools]

        return filtered

    def apply_to_copilot(self, config: dict[str, Any], tools: list[ToolSpec]) -> None:
        """Apply this ToolPolicy to a Copilot SDK session config in-place.

        Filters registered tools through ``allowed_tools``/``denied_tools``
        and sets ``config["tools"]`` to the filtered ToolSpec objects (the
        Copilot SDK requires objects with a ``.name`` attribute).  Also sets
        ``config["allowed_tools"]`` when native tools are blocked so the
        model can only call filtered tool names.
        """
        if not tools:
            return

        # Apply allow/deny filtering — keeps ToolSpec objects intact so
        # the Copilot SDK can access .name on each tool.
        filtered_tools: list[ToolSpec] = self.filter_tools(tools)
        config["tools"] = filtered_tools

        # When native tools are blocked, set an explicit allowlist so
        # only the filtered tool names are available to the model.
        if not self.allow_native:
            config["allowed_tools"] = [t.name for t in filtered_tools]

    def apply_to_openai(self, config: dict[str, Any], tools: list[ToolSpec]) -> None:
        """Apply this ToolPolicy to OpenAI backend options in-place.

        Filters the tools list through the policy and stores the result.
        """
        if not tools:
            return

        filtered = self.filter_tools(tools)
        config["tools"] = filtered

    def apply_to_claude(self, config: dict[str, Any], tools: list[ToolSpec]) -> None:
        """Apply this ToolPolicy to Claude backend options in-place.

        Claude uses MCP-style naming (``mcp__<server>__<name>`` — the
        server name comes from ``obscura.providers.claude.MCP_SERVER_NAME``),
        so the allowed-tools list is mapped accordingly.
        """
        if not tools:
            return

        if self.allow_native and not self.denied_tools and not self.allowed_tools:
            # allow_all — no restriction needed
            return

        # Local import avoids a circular dep with ``obscura.providers.claude``.
        from obscura.providers.claude import MCP_TOOL_PREFIX

        filtered = self.filter_tools(tools)
        config["allowed_tools"] = [f"{MCP_TOOL_PREFIX}{t.name}" for t in filtered]

    @override
    def __repr__(self) -> str:
        """String representation for debugging."""
        parts = []
        if not self.allow_native:
            parts.append("native=blocked")
        else:
            parts.append("native=allowed")

        if self.allowed_tools:
            parts.append(f"allowed={self.allowed_tools}")
        if self.denied_tools:
            parts.append(f"denied={self.denied_tools}")

        return f"ToolPolicy({', '.join(parts)})"
