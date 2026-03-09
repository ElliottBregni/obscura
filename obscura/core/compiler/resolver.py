"""obscura.core.compiler.resolver — Resolve references in spec graphs.

Phase 2 of the compile pipeline: follow template extends chains,
resolve policy refs, look up agent templates.

Enforces single-level extends (a template that extends another template
which itself extends is rejected).
"""

from __future__ import annotations

from typing import Any

from obscura.core.compiler.errors import ResolutionError
from obscura.core.compiler.loader import SpecRegistry
from obscura.core.compiler.specs import (
    PackSpec,
    PluginFilterSpec,
    PolicySpec,
    TemplateSpec,
    WorkspaceAgentRef,
    WorkspaceSpec,
    WorkspaceSpecBody,
)


def resolve_template_chain(
    template: TemplateSpec,
    registry: SpecRegistry,
) -> list[TemplateSpec]:
    """Return the template inheritance chain, base-first.

    Enforces max depth of 1 (child may extend one parent, parent may not
    extend anything).

    Returns
    -------
    list[TemplateSpec]
        ``[base, child]`` if extends is set, otherwise ``[template]``.

    Raises
    ------
    ResolutionError
        If the parent template is not found or the chain is too deep.
    """
    parent_name = template.spec.extends
    if parent_name is None:
        return [template]

    parent = registry.get_template(parent_name)
    if parent is None:
        raise ResolutionError(
            f"Template '{template.metadata.name}' extends '{parent_name}' "
            f"which was not found in the registry",
            source=template.metadata.name,
        )

    if parent.spec.extends is not None:
        raise ResolutionError(
            f"Template '{template.metadata.name}' extends '{parent_name}' "
            f"which itself extends '{parent.spec.extends}'. "
            f"Max inheritance depth is 1.",
            source=template.metadata.name,
        )

    return [parent, template]


def resolve_workspace_policies(
    workspace: WorkspaceSpec,
    registry: SpecRegistry,
) -> list[PolicySpec]:
    """Resolve all policy references in a workspace.

    Raises
    ------
    ResolutionError
        If a referenced policy is not found.
    """
    policies: list[PolicySpec] = []
    for name in workspace.spec.policies:
        policy = registry.get_policy(name)
        if policy is None:
            raise ResolutionError(
                f"Workspace '{workspace.metadata.name}' references policy "
                f"'{name}' which was not found",
                source=workspace.metadata.name,
            )
        policies.append(policy)
    return policies


def resolve_workspace_agent_template(
    agent_ref: WorkspaceAgentRef,
    registry: SpecRegistry,
    workspace_name: str,
) -> TemplateSpec:
    """Resolve the template for a workspace agent reference.

    Raises
    ------
    ResolutionError
        If the template is not found.
    """
    template = registry.get_template(agent_ref.template)
    if template is None:
        raise ResolutionError(
            f"Agent '{agent_ref.name}' in workspace '{workspace_name}' "
            f"references template '{agent_ref.template}' which was not found",
            source=workspace_name,
        )
    return template


def expand_workspace_packs(
    workspace: WorkspaceSpec,
    registry: SpecRegistry,
) -> WorkspaceSpec:
    """Expand pack references into the workspace spec.

    For each pack in ``workspace.spec.packs``, merges the pack's plugins,
    policies, config, and instructions into the workspace.  Returns a new
    WorkspaceSpec with packs fully expanded.

    Merge rules:
    - plugins: pack plugins are unioned into workspace plugins.include
    - policies: pack policies are appended (before workspace's own)
    - config: deep-merged, later packs override earlier, workspace wins
    - instructions: concatenated, stored in config["_pack_instructions"]
    - capabilities: accumulated in config["_pack_capabilities"]
    - templates: advisory only (not auto-instantiated)

    Raises
    ------
    ResolutionError
        If a referenced pack is not found.
    """
    pack_names = workspace.spec.packs
    if not pack_names:
        return workspace

    # Accumulate contributions from all packs
    all_plugins: list[str] = []
    all_policies: list[str] = []
    all_instructions: list[str] = []
    all_cap_grants: list[str] = []
    all_cap_denials: list[str] = []
    merged_config: dict[str, Any] = {}

    for pack_name in pack_names:
        pack = registry.get_pack(pack_name)
        if pack is None:
            raise ResolutionError(
                f"Workspace '{workspace.metadata.name}' references pack "
                f"'{pack_name}' which was not found",
                source=workspace.metadata.name,
            )

        all_plugins.extend(pack.spec.plugins)
        all_policies.extend(pack.spec.policies)

        if pack.spec.instructions.strip():
            all_instructions.append(pack.spec.instructions.strip())

        all_cap_grants.extend(pack.spec.capabilities.grant)
        all_cap_denials.extend(pack.spec.capabilities.deny)

        # Deep-merge pack config (later packs override earlier)
        _deep_merge_dict(merged_config, pack.spec.config)

    # Merge workspace's own config on top (workspace wins)
    _deep_merge_dict(merged_config, dict(workspace.spec.config))

    # Store pack metadata in config for downstream consumption
    if all_instructions:
        merged_config["_pack_instructions"] = "\n\n".join(all_instructions)
    if all_cap_grants or all_cap_denials:
        merged_config["_pack_capabilities"] = {
            "grant": list(dict.fromkeys(all_cap_grants)),  # dedup preserving order
            "deny": list(dict.fromkeys(all_cap_denials)),
        }

    # Union pack plugins into workspace plugins.include
    ws_include = list(workspace.spec.plugins.include)
    for p in all_plugins:
        if p not in ws_include:
            ws_include.append(p)

    # Prepend pack policies before workspace policies (workspace wins on conflict)
    ws_policies = list(workspace.spec.policies)
    for p in all_policies:
        if p not in ws_policies:
            ws_policies.insert(len(ws_policies) - len(workspace.spec.policies), p)

    # Build new workspace spec with expanded values
    new_plugins = PluginFilterSpec(
        include=ws_include,
        exclude=list(workspace.spec.plugins.exclude),
    )

    new_body = WorkspaceSpecBody(
        packs=[],  # packs are now expanded
        config=merged_config,
        policies=ws_policies,
        plugins=new_plugins,
        memory=workspace.spec.memory,
        agents=list(workspace.spec.agents),
        startup=workspace.spec.startup,
    )

    return WorkspaceSpec(
        api_version=workspace.api_version,
        kind="Workspace",
        metadata=workspace.metadata,
        spec=new_body,
    )


def _deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> None:
    """Recursively merge *override* into *base* (mutates *base*)."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge_dict(base[key], value)
        else:
            base[key] = value
