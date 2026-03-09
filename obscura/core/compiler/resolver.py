"""obscura.core.compiler.resolver — Resolve references in spec graphs.

Phase 2 of the compile pipeline: follow template extends chains,
resolve policy refs, look up agent templates.

Enforces single-level extends (a template that extends another template
which itself extends is rejected).
"""

from __future__ import annotations

from obscura.core.compiler.errors import ResolutionError
from obscura.core.compiler.loader import SpecRegistry
from obscura.core.compiler.specs import (
    PolicySpec,
    TemplateSpec,
    WorkspaceAgentRef,
    WorkspaceSpec,
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
