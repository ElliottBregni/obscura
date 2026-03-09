"""obscura.core.compiler.merger — Merge specs according to precedence rules.

Phase 3 of the compile pipeline: apply overrides and produce merged values.

Precedence (lowest to highest):
    base template < child template < agent overrides < workspace config < CLI flags

For plugins/capabilities (lists): child extends parent (union).
For scalars (provider, max_iterations): child overrides parent.
For None vs list: None means "all allowed", list means "only these".
For dicts (config): deep merge, child wins on conflicts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from obscura.core.compiler.compiled import (
    CompiledAgent,
    CompiledMCPServer,
    CompiledMemory,
    CompiledPolicy,
)
from obscura.core.compiler.specs import (
    MCPServerSpec,
    PolicySpec,
    SpecMetadata,
    TemplateSpec,
    TemplateSpecBody,
    WorkspaceAgentRef,
    WorkspaceSpec,
)


def merge_template_chain(chain: list[TemplateSpec]) -> TemplateSpec:
    """Merge a base-first template chain into a single effective template.

    For a chain ``[base, child]``, the child's explicit values override
    the base's defaults. Lists (plugins, capabilities) are unioned.
    """
    if len(chain) == 1:
        return chain[0]

    base = chain[0]
    child = chain[1]

    # Lists: union (child appends to base, deduped)
    merged_plugins = _merge_str_lists(base.spec.plugins, child.spec.plugins)
    merged_caps = _merge_str_lists(base.spec.capabilities, child.spec.capabilities)
    merged_denylist = _merge_str_lists(base.spec.tool_denylist, child.spec.tool_denylist)

    # MCP servers: child extends base (by name, child wins on conflict)
    base_mcp: dict[str, MCPServerSpec] = {s.name: s for s in base.spec.mcp_servers}
    for s in child.spec.mcp_servers:
        base_mcp[s.name] = s
    merged_mcp: list[MCPServerSpec] = list(base_mcp.values())

    # Allowlist: child overrides if set, otherwise inherit base
    merged_allowlist = (
        child.spec.tool_allowlist
        if child.spec.tool_allowlist is not None
        else base.spec.tool_allowlist
    )

    # Config: deep merge
    merged_config = _deep_merge(base.spec.config, child.spec.config)

    # Scalars: child overrides (use child value, which may be the default)
    merged_body = TemplateSpecBody(
        extends=None,  # chain is resolved
        agent_type=child.spec.agent_type,
        max_iterations=child.spec.max_iterations,
        provider=child.spec.provider,
        model_id=child.spec.model_id if child.spec.model_id else base.spec.model_id,
        instructions=_merge_instructions(
            base.spec.instructions, child.spec.instructions,
        ),
        plugins=merged_plugins,
        capabilities=merged_caps,
        tool_allowlist=merged_allowlist,
        tool_denylist=merged_denylist,
        mcp_servers=merged_mcp,
        config=merged_config,
        input_schema=child.spec.input_schema or base.spec.input_schema,
    )

    return TemplateSpec(
        api_version=child.api_version,
        kind="Template",
        metadata=SpecMetadata(
            name=child.metadata.name,
            description=child.metadata.description or base.metadata.description,
            tags=_merge_str_lists(base.metadata.tags, child.metadata.tags),
        ),
        spec=merged_body,
    )


def apply_agent_overrides(
    template: TemplateSpec,
    agent_ref: WorkspaceAgentRef,
) -> TemplateSpec:
    """Apply agent-level overrides on top of a resolved template."""
    overrides: dict[str, Any] = agent_ref.overrides
    if not overrides:
        return template

    spec = template.spec
    ov_plugins: Any = overrides.get("plugins")
    ov_config: Any = overrides.get("config")

    new_plugins: list[str] = (
        _merge_str_lists(spec.plugins, list(ov_plugins))
        if isinstance(ov_plugins, list)
        else list(spec.plugins)
    )
    new_config: dict[str, Any] = (
        _deep_merge(dict(spec.config), dict(ov_config))
        if isinstance(ov_config, dict)
        else dict(spec.config)
    )

    merged_body = TemplateSpecBody(
        extends=None,
        agent_type=overrides.get("agent_type", spec.agent_type),
        max_iterations=overrides.get("max_iterations", spec.max_iterations),
        provider=overrides.get("provider", spec.provider),
        model_id=overrides.get("model_id", spec.model_id),
        instructions=overrides.get("instructions", spec.instructions),
        plugins=new_plugins,
        capabilities=overrides.get("capabilities", spec.capabilities),
        tool_allowlist=overrides.get("tool_allowlist", spec.tool_allowlist),
        tool_denylist=overrides.get("tool_denylist", spec.tool_denylist),
        mcp_servers=spec.mcp_servers,
        config=new_config,
        input_schema=spec.input_schema,
    )

    return TemplateSpec(
        api_version=template.api_version,
        kind="Template",
        metadata=SpecMetadata(
            name=agent_ref.name,
            description=template.metadata.description,
            tags=list(template.metadata.tags),
        ),
        spec=merged_body,
    )


def compile_policy(policy_spec: PolicySpec) -> CompiledPolicy:
    """Convert a PolicySpec into a frozen CompiledPolicy."""
    s = policy_spec.spec
    return CompiledPolicy(
        name=policy_spec.metadata.name,
        tool_allowlist=(
            frozenset(s.tool_allowlist) if s.tool_allowlist is not None else None
        ),
        tool_denylist=frozenset(s.tool_denylist),
        require_confirmation=frozenset(s.require_confirmation),
        plugin_allowlist=(
            frozenset(s.plugin_allowlist) if s.plugin_allowlist is not None else None
        ),
        plugin_denylist=frozenset(s.plugin_denylist),
        max_turns=s.max_turns,
        token_budget=s.token_budget,
        base_dir=Path(s.base_dir) if s.base_dir else None,
        allow_dynamic_tools=s.allow_dynamic_tools,
    )


def compile_agent(
    template: TemplateSpec,
    agent_ref: WorkspaceAgentRef,
    policies: list[CompiledPolicy],
    workspace_plugins: tuple[list[str], list[str]],
) -> CompiledAgent:
    """Compile a resolved template + agent ref into a CompiledAgent.

    Applies plugin filtering:
        template plugins + agent plugin additions
        filtered by workspace include/exclude
        filtered by policy allow/deny
    """
    spec = template.spec
    ws_include: list[str] = workspace_plugins[0]
    ws_exclude: list[str] = workspace_plugins[1]

    # Start with template plugins
    effective_plugins: list[str] = list(spec.plugins)

    # Filter by workspace include (if non-empty, only those are available)
    if ws_include:
        effective_plugins = [p for p in effective_plugins if p in ws_include]

    # Filter by workspace exclude
    if ws_exclude:
        effective_plugins = [p for p in effective_plugins if p not in ws_exclude]

    # Filter by policy plugin restrictions
    for policy in policies:
        if policy.plugin_allowlist is not None:
            effective_plugins = [
                p for p in effective_plugins if p in policy.plugin_allowlist
            ]
        effective_plugins = [
            p for p in effective_plugins if p not in policy.plugin_denylist
        ]

    # Compute effective tool restrictions (policy has last word)
    tool_allowlist = (
        frozenset(spec.tool_allowlist) if spec.tool_allowlist is not None else None
    )
    tool_denylist = frozenset(spec.tool_denylist)

    for policy in policies:
        if policy.tool_allowlist is not None:
            if tool_allowlist is not None:
                tool_allowlist = tool_allowlist & policy.tool_allowlist
            else:
                tool_allowlist = policy.tool_allowlist
        tool_denylist = tool_denylist | policy.tool_denylist

    # Compile MCP servers
    mcp_servers = tuple(
        CompiledMCPServer(
            name=s.name,
            transport=s.transport,
            command=s.command,
            args=tuple(s.args),
            env=tuple(s.env.items()),
        )
        for s in spec.mcp_servers
    )

    return CompiledAgent(
        name=agent_ref.name,
        template_name=template.metadata.name,
        mode=agent_ref.mode,
        agent_type=spec.agent_type,
        provider=spec.provider,
        model_id=spec.model_id,
        instructions=spec.instructions,
        max_iterations=spec.max_iterations,
        plugins=tuple(effective_plugins),
        capabilities=tuple(spec.capabilities),
        tool_allowlist=tool_allowlist,
        tool_denylist=tool_denylist,
        mcp_servers=mcp_servers,
        config=dict(spec.config),
        input_vars=dict(agent_ref.input),
    )


def compile_memory(workspace: WorkspaceSpec) -> CompiledMemory | None:
    """Compile memory binding from workspace spec."""
    mem = workspace.spec.memory
    if mem is None:
        return None
    return CompiledMemory(
        namespace=mem.namespace,
        shared_scope=mem.shared_scope,
        stores=tuple(mem.stores),
        retention_days=mem.retention_days,
    )


def compile_workspace_config(workspace: WorkspaceSpec) -> dict[str, Any]:
    """Extract and return workspace-level config."""
    return dict(workspace.spec.config)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _merge_str_lists(base: list[str], child: list[str]) -> list[str]:
    """Union two string lists preserving order, deduped."""
    seen: set[str] = set()
    result: list[str] = []
    for item in base + child:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _merge_instructions(base: str, child: str) -> str:
    """Merge instruction strings: child appends to base with separator."""
    base = base.strip()
    child = child.strip()
    if not base:
        return child
    if not child:
        return base
    return base + "\n\n" + child


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge two dicts. Override wins on scalar conflicts."""
    result = dict(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
