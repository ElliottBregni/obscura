"""Internal configuration models — replacing ``dict[str, Any]`` parameters.

This module owns the typed shapes for the dictionaries passed around the
runtime today: agent configuration, MCP server specs from
``~/.obscura/mcp/core.json``, hook callback payloads, the bash classifier
result, and plugin manifest TOMLs from ``~/.obscura/plugins/builtins``.

Boundary configs (``MCPServerSpec``, ``PluginManifest``) inherit from
``BoundaryModel`` so unknown keys in on-disk JSON/TOML do not break startup
when the schema gains new fields.  In-memory configs (``AgentConfig``,
``HookContext``, ``BashClassification``) inherit from ``ObscuraModel`` so
constructor mistakes fail fast.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ConfigDict, Field

from obscura.core.enums.agent import AgentPhase, Backend, ExecutionMode, HookPoint
from obscura.core.enums.protocol import MCPTransport
from obscura.core.enums.tools import BashRisk
from obscura.core.models._base import BoundaryModel, ObscuraModel
from obscura.core.models._mixins import MetadataMixin


class AgentConfig(ObscuraModel, MetadataMixin):
    """Typed agent configuration replacing the loose ``cfg`` dict.

    Mirrors the keys read from ``agents.yaml``/agent definitions in
    ``tools/swarm.py`` and ``agent/supervisor.py``.  Optional fields fall
    back to ``None``/empty so partial dicts still validate; downstream
    code keeps the same ``cfg.get("name", "default")`` semantics by reading
    the typed field.

    Override of ``ObscuraModel.model_config`` relaxes ``strict=False``
    because this model is constructed directly from on-disk YAML/TOML
    dicts where enum fields arrive as bare strings; ``extra="forbid"`` and
    ``frozen=True`` are preserved so unknown keys and post-construction
    mutation still fail fast.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=False,
        validate_assignment=True,
        use_enum_values=False,
    )

    name: str
    provider: Backend = Backend.COPILOT
    model_id: str | None = None
    system_prompt: str | None = None
    timeout_seconds: int | None = None
    max_iterations: int | None = None
    max_turns: int | None = None
    mode: ExecutionMode = ExecutionMode.UNIFIED
    enabled: bool = True
    tools: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] | str = ()
    can_delegate: bool = False
    delegate_allowlist: tuple[str, ...] = ()
    max_delegation_depth: int = 3


class MCPServerSpec(BoundaryModel):
    """One entry inside ``~/.obscura/mcp/core.json``.

    Boundary model: tolerates forward-compat keys we do not understand yet
    (``extra="ignore"`` from ``BoundaryModel``).  Both ``transport`` and
    legacy ``type`` aliases populate this field — runtime callers should
    construct via ``model_validate`` after coercing aliases at the loader.
    """

    name: str
    transport: MCPTransport = MCPTransport.STDIO
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: Mapping[str, str] = Field(default_factory=dict)
    tools: tuple[str, ...] = ()


class HookContext(ObscuraModel):
    """Payload handed to hook callbacks.

    Key set is derived from the JSON envelope assembled in
    ``core/hooks.py`` (``_make_command_before_hook`` /
    ``_make_command_after_hook``) plus the additional metadata callers
    inspect on ``AgentEvent`` itself.  ``block`` is typed as ``Any`` to
    avoid a forward reference to the ``ContentBlock`` union owned by
    Team Core Types.
    """

    event: str
    session_id: str | None = None
    phase: AgentPhase | None = None
    hook_point: HookPoint | None = None
    tool_name: str | None = None
    tool_input: Mapping[str, Any] | None = None
    tool_result: str | None = None
    block: Any = None


class BashClassification(ObscuraModel):
    """Output of ``core/bash_classifier.py``.

    Mirrors the legacy ``Classification`` dataclass.  The dataclass stays
    in place for backwards-compatible imports; this Pydantic shape is
    available for callers that prefer typed validation at the seam.
    """

    risk: BashRisk
    reasons: tuple[str, ...] = ()
    dangerous_patterns: tuple[str, ...] = ()
    latency_ms: int = 0


class PluginManifest(BoundaryModel):
    """Typed read of ``~/.obscura/plugins/builtins/<id>.toml``.

    Only the top-level metadata is modelled here — capability, tool, and
    bootstrap entries live as raw mappings so the loader's existing
    coercion path (``plugins/loader.py``) keeps owning the heavy lifting.
    Fields cover every key actually emitted by the bundled TOML manifests.
    """

    id: str
    name: str
    version: str
    source_type: str = "builtin"
    runtime_type: str = "sdk"
    trust_level: str = "community"
    author: str = ""
    description: str = ""
    capabilities: tuple[Mapping[str, Any], ...] = ()
    tools: tuple[Mapping[str, Any], ...] = ()
    workflows: tuple[Mapping[str, Any], ...] = ()
    instructions: tuple[Mapping[str, Any], ...] = ()
    policy_hints: tuple[Mapping[str, Any], ...] = ()
    config_requirements: tuple[Mapping[str, Any], ...] = ()
    install_hook: str | None = None
    bootstrap_hook: str | None = None
    bootstrap: Mapping[str, Any] | None = None
    healthcheck: Mapping[str, Any] | None = None


__all__ = [
    "AgentConfig",
    "BashClassification",
    "HookContext",
    "MCPServerSpec",
    "PluginManifest",
]
