"""obscura.core.compiler.validator — Validate compiled output.

Phase 4 of the compile pipeline: check that the compiled workspace is
internally consistent and references are satisfiable.
"""

from __future__ import annotations

import logging

from obscura.core.compiler.compiled import CompiledAgent, CompiledWorkspace
from obscura.core.compiler.errors import SpecValidationError
from obscura.core.compiler.loader import SpecRegistry

logger = logging.getLogger(__name__)

# Valid agent modes
VALID_MODES = frozenset({"task", "daemon", "reactive", "scheduled"})

# Valid agent types (execution strategies)
VALID_AGENT_TYPES = frozenset({"loop", "daemon", "aper"})


def validate_workspace(
    workspace: CompiledWorkspace,
    *,
    available_plugins: frozenset[str] | None = None,
) -> list[SpecValidationError]:
    """Validate a compiled workspace, returning all errors found.

    Parameters
    ----------
    workspace:
        The compiled workspace to validate.
    available_plugins:
        Set of plugin IDs known to be installed. If None, plugin
        availability is not checked.

    Returns
    -------
    list[SpecValidationError]
        All validation errors found. Empty list means valid.
    """
    errors: list[SpecValidationError] = []

    # Check startup agents exist in the workspace
    agent_names = {a.name for a in workspace.agents}
    for name in workspace.startup_agents:
        if name not in agent_names:
            errors.append(SpecValidationError(
                f"Startup agent '{name}' is not defined in workspace "
                f"'{workspace.name}'",
                source=workspace.name,
            ))

    # Validate each agent
    for agent in workspace.agents:
        errors.extend(_validate_agent(agent, available_plugins))

    # Check for duplicate agent names
    seen_names: set[str] = set()
    for agent in workspace.agents:
        if agent.name in seen_names:
            errors.append(SpecValidationError(
                f"Duplicate agent name '{agent.name}' in workspace "
                f"'{workspace.name}'",
                source=workspace.name,
            ))
        seen_names.add(agent.name)

    return errors


def _validate_agent(
    agent: CompiledAgent,
    available_plugins: frozenset[str] | None,
) -> list[SpecValidationError]:
    """Validate a single compiled agent."""
    errors: list[SpecValidationError] = []

    if agent.mode not in VALID_MODES:
        errors.append(SpecValidationError(
            f"Agent '{agent.name}' has invalid mode '{agent.mode}'. "
            f"Must be one of: {sorted(VALID_MODES)}",
            source=agent.name,
        ))

    if agent.agent_type not in VALID_AGENT_TYPES:
        errors.append(SpecValidationError(
            f"Agent '{agent.name}' has invalid agent_type '{agent.agent_type}'. "
            f"Must be one of: {sorted(VALID_AGENT_TYPES)}",
            source=agent.name,
        ))

    if agent.max_iterations < 1:
        errors.append(SpecValidationError(
            f"Agent '{agent.name}' has max_iterations={agent.max_iterations}, "
            f"must be >= 1",
            source=agent.name,
        ))

    # Check plugin availability
    if available_plugins is not None:
        for plugin in agent.plugins:
            if plugin not in available_plugins:
                errors.append(SpecValidationError(
                    f"Agent '{agent.name}' requires plugin '{plugin}' "
                    f"which is not available",
                    source=agent.name,
                ))

    # Check tool allowlist/denylist consistency
    if agent.tool_allowlist is not None and agent.tool_denylist:
        overlap = agent.tool_allowlist & agent.tool_denylist
        if overlap:
            errors.append(SpecValidationError(
                f"Agent '{agent.name}' has tools in both allowlist and "
                f"denylist: {sorted(overlap)}",
                source=agent.name,
            ))

    return errors


def validate_pack_references(
    pack_names: tuple[str, ...],
    workspace_name: str,
    registry: SpecRegistry,
    *,
    available_plugins: frozenset[str] | None = None,
) -> list[SpecValidationError]:
    """Validate that all pack references and their contents are satisfiable.

    Checks:
    - Each pack name resolves to a PackSpec in the registry
    - Each pack's plugins exist in available_plugins (if provided)
    - Each pack's policies exist in the registry
    - Each pack's templates exist in the registry
    """
    errors: list[SpecValidationError] = []

    for pack_name in pack_names:
        pack = registry.get_pack(pack_name)
        if pack is None:
            errors.append(SpecValidationError(
                f"Workspace '{workspace_name}' references pack "
                f"'{pack_name}' which was not found",
                source=workspace_name,
            ))
            continue

        # Check pack plugins
        if available_plugins is not None:
            for plugin in pack.spec.plugins:
                if plugin not in available_plugins:
                    errors.append(SpecValidationError(
                        f"Pack '{pack_name}' requires plugin '{plugin}' "
                        f"which is not available",
                        source=pack_name,
                    ))

        # Check pack policies
        for policy_name in pack.spec.policies:
            if registry.get_policy(policy_name) is None:
                errors.append(SpecValidationError(
                    f"Pack '{pack_name}' references policy "
                    f"'{policy_name}' which was not found",
                    source=pack_name,
                ))

        # Check pack templates
        for template_name in pack.spec.templates:
            if registry.get_template(template_name) is None:
                errors.append(SpecValidationError(
                    f"Pack '{pack_name}' references template "
                    f"'{template_name}' which was not found",
                    source=pack_name,
                ))

    return errors
