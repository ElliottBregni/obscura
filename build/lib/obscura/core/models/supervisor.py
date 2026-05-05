"""Supervisor-domain Pydantic models — typed shapes for agent templating,
policy versioning, and tool snapshots.

Migrated from the loose ``dict[str, Any]`` payloads previously held by the
:mod:`obscura.core.supervisor.agent_templates`,
:mod:`obscura.core.supervisor.policy_store`, and
:mod:`obscura.core.supervisor.tool_snapshot` modules.

Templates and rendered versions persist their JSON to SQLite with
``json.dumps(..., sort_keys=True)`` so byte-for-byte serialization parity
matters: the boundary models below retain ``extra="ignore"`` from
``BoundaryModel`` for forward-compatible loads but emit only known fields
on ``model_dump`` — and templates may carry arbitrary placeholder
extensions, which the ``extras`` map preserves.

These models intentionally do NOT subclass :class:`MetadataMixin` because
the persisted SQLite columns include ``template_json`` / ``policy_json``
as the canonical free-form payload — adding a separate metadata mapping
would conflict with the wire format.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from pydantic import Field

from obscura.core.models._base import BoundaryModel, ObscuraModel


# ---------------------------------------------------------------------------
# Agent template body
# ---------------------------------------------------------------------------


class AgentTemplateBody(BoundaryModel):
    """The on-disk JSON body of an :class:`AgentTemplate`.

    Persisted to ``agent_templates.template_json`` as
    ``json.dumps(payload, sort_keys=True)``; tolerated extra keys allow
    forward-compat additions (custom placeholders, runtime hints) without
    forcing a schema migration.

    Both ``system_prompt`` and ``tool_bundles`` are first-class because
    every existing in-tree template names them; richer ``extras`` carries
    domain-specific keys (``safety_profile``, ``tool_overrides``, …) so
    no existing on-disk payload loses fidelity.
    """

    system_prompt: str = ""
    tool_bundles: tuple[str, ...] = ()
    extras: Mapping[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> AgentTemplateBody:
        """Coerce a raw on-disk dict into a typed body, preserving unknowns.

        Unknown top-level keys are funnelled into ``extras`` so the on-disk
        payload round-trips through :meth:`to_mapping` unchanged.
        """
        if not raw:
            return cls()
        known = {"system_prompt", "tool_bundles"}
        system_prompt_raw = raw.get("system_prompt", "")
        bundles_raw = raw.get("tool_bundles", [])
        extras = {k: v for k, v in raw.items() if k not in known}
        bundles: tuple[str, ...]
        if isinstance(bundles_raw, (list, tuple)):
            bundles = tuple(str(b) for b in cast("list[Any]", bundles_raw))
        else:
            bundles = ()
        return cls(
            system_prompt=str(system_prompt_raw or ""),
            tool_bundles=bundles,
            extras=dict(extras),
        )

    def to_mapping(self) -> dict[str, Any]:
        """Serialize back to the wire-format dict shape, byte-stable.

        Emits the canonical keys plus any preserved ``extras``. Result
        order is irrelevant because callers always re-encode with
        ``json.dumps(..., sort_keys=True)``.
        """
        out: dict[str, Any] = dict(self.extras)
        out["system_prompt"] = self.system_prompt
        out["tool_bundles"] = list(self.tool_bundles)
        return out


# ---------------------------------------------------------------------------
# Rendered agent version body
# ---------------------------------------------------------------------------


class AgentVersionBody(BoundaryModel):
    """Rendered, frozen agent definition — the body of an :class:`AgentVersion`.

    Persisted to ``agent_versions.render_json``. Same shape as
    :class:`AgentTemplateBody` minus placeholders (``{{vars}}`` are
    already resolved); ``tools`` lists the concrete tool names the version
    was rendered with.
    """

    system_prompt: str = ""
    tools: tuple[str, ...] = ()
    extras: Mapping[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> AgentVersionBody:
        if not raw:
            return cls()
        known = {"system_prompt", "tools"}
        system_prompt_raw = raw.get("system_prompt", "")
        tools_raw = raw.get("tools", [])
        extras = {k: v for k, v in raw.items() if k not in known}
        tools: tuple[str, ...]
        if isinstance(tools_raw, (list, tuple)):
            tools = tuple(str(t) for t in cast("list[Any]", tools_raw))
        else:
            tools = ()
        return cls(
            system_prompt=str(system_prompt_raw or ""),
            tools=tools,
            extras=dict(extras),
        )

    def to_mapping(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.extras)
        out["system_prompt"] = self.system_prompt
        out["tools"] = list(self.tools)
        return out


# ---------------------------------------------------------------------------
# Policy body
# ---------------------------------------------------------------------------


class PolicyBody(BoundaryModel):
    """Body of a :class:`PolicyVersion` — budgets, allowlists, and gates.

    Persisted to ``policy_versions.policy_json``. Boundary model because
    the wire format may grow new keys; unknown keys are preserved in
    ``extras`` so the round-trip is loss-free.
    """

    tool_allowlist: tuple[str, ...] | None = None
    tool_denylist: tuple[str, ...] = ()
    require_confirmation: tuple[str, ...] = ()
    max_turns: int = 10
    token_budget: int = 0
    allow_dynamic_tools: bool = False
    extras: Mapping[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> PolicyBody:
        if not raw:
            return cls()
        known = {
            "tool_allowlist",
            "tool_denylist",
            "require_confirmation",
            "max_turns",
            "token_budget",
            "allow_dynamic_tools",
        }
        extras = {k: v for k, v in raw.items() if k not in known}

        allow_raw = raw.get("tool_allowlist")
        allowlist: tuple[str, ...] | None
        if allow_raw is None:
            allowlist = None
        elif isinstance(allow_raw, (list, tuple)):
            allowlist = tuple(str(t) for t in cast("list[Any]", allow_raw))
        else:
            allowlist = None

        deny_raw = raw.get("tool_denylist", [])
        denylist: tuple[str, ...] = (
            tuple(str(t) for t in cast("list[Any]", deny_raw))
            if isinstance(deny_raw, (list, tuple))
            else ()
        )

        confirm_raw = raw.get("require_confirmation", [])
        confirmation: tuple[str, ...] = (
            tuple(str(t) for t in cast("list[Any]", confirm_raw))
            if isinstance(confirm_raw, (list, tuple))
            else ()
        )

        return cls(
            tool_allowlist=allowlist,
            tool_denylist=denylist,
            require_confirmation=confirmation,
            max_turns=int(raw.get("max_turns", 10) or 10),
            token_budget=int(raw.get("token_budget", 0) or 0),
            allow_dynamic_tools=bool(raw.get("allow_dynamic_tools", False)),
            extras=dict(extras),
        )

    def to_mapping(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.extras)
        out["tool_allowlist"] = (
            None if self.tool_allowlist is None else list(self.tool_allowlist)
        )
        out["tool_denylist"] = list(self.tool_denylist)
        out["require_confirmation"] = list(self.require_confirmation)
        out["max_turns"] = self.max_turns
        out["token_budget"] = self.token_budget
        out["allow_dynamic_tools"] = self.allow_dynamic_tools
        return out


# ---------------------------------------------------------------------------
# Tool schema (frozen tool entry parameters)
# ---------------------------------------------------------------------------


class ToolSchema(ObscuraModel):
    """A frozen tool's JSON-Schema parameters.

    The tool snapshot path (``FrozenToolEntry.parameters``) accepts
    arbitrary JSON-Schema documents; storing them in a typed wrapper
    catches misuse (e.g. passing a list) at the seam without losing
    fidelity for legitimate schemas.
    """

    parameters: Mapping[str, Any] = Field(default_factory=dict)


__all__ = [
    "AgentTemplateBody",
    "AgentVersionBody",
    "PolicyBody",
    "ToolSchema",
]
