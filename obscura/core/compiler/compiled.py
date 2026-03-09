"""obscura.core.compiler.compiled — Frozen compiled output models.

These are the runtime-ready objects produced by the compile pipeline.
Immutable after creation, hashable, safe to pass across threads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _empty_frozenset() -> frozenset[str]:
    return frozenset()


def _empty_tuple_str() -> tuple[str, ...]:
    return ()


def _empty_dict_str_any() -> dict[str, Any]:
    return {}


# ---------------------------------------------------------------------------
# Compiled MCP server
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledMCPServer:
    """A fully resolved MCP server binding."""

    name: str
    transport: str
    command: str
    args: tuple[str, ...]
    env: tuple[tuple[str, str], ...]  # tuple of (key, value) pairs


# ---------------------------------------------------------------------------
# Compiled policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledPolicy:
    """A fully resolved, merged policy ready for enforcement."""

    name: str
    tool_allowlist: frozenset[str] | None = None
    tool_denylist: frozenset[str] = field(default_factory=_empty_frozenset)
    require_confirmation: frozenset[str] = field(default_factory=_empty_frozenset)
    plugin_allowlist: frozenset[str] | None = None
    plugin_denylist: frozenset[str] = field(default_factory=_empty_frozenset)
    max_turns: int = 25
    token_budget: int = 0
    base_dir: Path | None = None
    allow_dynamic_tools: bool = False


# ---------------------------------------------------------------------------
# Compiled memory binding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledMemory:
    """A resolved memory binding for a workspace."""

    namespace: str
    shared_scope: str
    stores: tuple[str, ...] = field(default_factory=_empty_tuple_str)
    retention_days: int = 30


# ---------------------------------------------------------------------------
# Compiled agent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledAgent:
    """A fully resolved agent ready to be spawned by the runtime."""

    name: str
    template_name: str
    mode: str
    agent_type: str
    provider: str
    model_id: str | None = None
    instructions: str = ""
    max_iterations: int = 25
    plugins: tuple[str, ...] = field(default_factory=_empty_tuple_str)
    capabilities: tuple[str, ...] = field(default_factory=_empty_tuple_str)
    tool_allowlist: frozenset[str] | None = None
    tool_denylist: frozenset[str] = field(default_factory=_empty_frozenset)
    mcp_servers: tuple[CompiledMCPServer, ...] = field(default_factory=tuple)
    config: dict[str, Any] = field(default_factory=_empty_dict_str_any)
    input_vars: dict[str, Any] = field(default_factory=_empty_dict_str_any)


# ---------------------------------------------------------------------------
# Compiled workspace
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompiledWorkspace:
    """Everything the runtime needs to boot a workspace."""

    name: str
    agents: tuple[CompiledAgent, ...] = field(default_factory=tuple)
    policies: tuple[CompiledPolicy, ...] = field(default_factory=tuple)
    memory: CompiledMemory | None = None
    plugin_include: frozenset[str] = field(default_factory=_empty_frozenset)
    plugin_exclude: frozenset[str] = field(default_factory=_empty_frozenset)
    config: dict[str, Any] = field(default_factory=_empty_dict_str_any)
    startup_agents: tuple[str, ...] = field(default_factory=_empty_tuple_str)
    preload_plugins: bool = True
