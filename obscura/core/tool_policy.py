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
    
    # From environment variable
    policy = ToolPolicy.from_env()
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from obscura.core.types import ToolSpec


@dataclass(frozen=True)
class ToolPolicy:
    """Controls which tools are available to the model across all backends.
    
    This policy provides unified tool access control that works consistently
    across Copilot, Claude, OpenAI, and other backends.
    
    Attributes:
        allow_native: If False, blocks backend's built-in/native tools (default: False)
        allowed_tools: Whitelist of specific tool names (None = allow all registered)
        denied_tools: Blacklist of specific tool names to block
        
    The policy is applied in this order:
    1. If allow_native=False, native backend tools are blocked
    2. If allowed_tools is set, only those tools are available
    3. If denied_tools is set, those tools are removed from the allowed set
    
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
    """
    
    allow_native: bool = False
    allowed_tools: list[str] | None = None
    denied_tools: list[str] | None = None
    
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
        ## TODO implement subagent-only policy:allow tools
        return cls(allow_native=False, allowed_tools=["run_shell"])
    

    
    # -- Utility methods ----------------------------------------------------
    
    def is_tool_allowed(self, tool_name: str, is_native: bool = False) -> bool:
        """Check if a specific tool is allowed by this policy.
        
        Args:
            tool_name: Name of the tool to check
            is_native: Whether this is a native backend tool
            
        Returns:
            True if the tool is allowed, False otherwise
            
        Example::
        
            policy = ToolPolicy.custom_only()
            policy.is_tool_allowed("search", is_native=False)  # True
            policy.is_tool_allowed("native_tool", is_native=True)  # False
        """
        # Check native tool restriction
        if is_native and not self.allow_native:
            return False
        
        # Check denied list first
        if self.denied_tools and tool_name in self.denied_tools:
            return False
        
        # Check allowed list
        if self.allowed_tools:
            return tool_name in self.allowed_tools
        
        # If no allowed list specified, allow by default (unless denied above)
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

        For Copilot, do not remove or block provider-native tools here — always
        allow native tools to be present in the session config. This method
        simply serializes given ToolSpec objects (or dicts) into the format
        expected by the Copilot SDK and places them on config["tools"].
        """
        # If no tools present, nothing to do
        if not tools:
            return

        # Serialize ToolSpec objects to dicts expected by the SDK without
        # attempting to heuristically remove native tools.
        serialized: list[dict[str, Any]] = []
        for t in tools:
            if isinstance(t, dict):
                serialized.append(t)
                continue
            serialized.append(
                {
                    "name": t.name,
                    "description": getattr(t, "description", ""),
                    "parameters": getattr(t, "parameters", {}),
                }
            )

        # Mutate the config in place
        config["tools"] = serialized
    
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
