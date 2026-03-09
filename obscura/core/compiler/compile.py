"""obscura.core.compiler.compile — Main compile entrypoint.

Orchestrates the full pipeline:
    load → resolve → merge → validate → produce runtime objects

Usage::

    from obscura.core.compiler import compile_workspace

    workspace = compile_workspace(
        workspace_name="code-mode",
        specs_dir=Path(".obscura/specs"),
    )
    # workspace is a CompiledWorkspace ready for the runtime
"""

from __future__ import annotations

import logging
from pathlib import Path

from obscura.core.compiler.compiled import CompiledAgent, CompiledWorkspace
from obscura.core.paths import resolve_obscura_specs_dir
from obscura.core.compiler.errors import ResolutionError, SpecValidationError
from obscura.core.compiler.loader import SpecRegistry, load_specs_dir
from obscura.core.compiler.merger import (
    apply_agent_overrides,
    compile_agent,
    compile_memory,
    compile_policy,
    compile_workspace_config,
    merge_template_chain,
)
from obscura.core.compiler.resolver import (
    resolve_template_chain,
    resolve_workspace_agent_template,
    resolve_workspace_policies,
)
from obscura.core.compiler.specs import (
    SpecMetadata,
    WorkspaceAgentRef,
    WorkspaceSpec,
    WorkspaceSpecBody,
)
from obscura.core.compiler.validator import validate_workspace

logger = logging.getLogger(__name__)


def compile_workspace_from_dir(
    workspace_name: str,
    specs_dir: Path,
    *,
    available_plugins: frozenset[str] | None = None,
    strict: bool = True,
) -> CompiledWorkspace:
    """Full compile pipeline: load all specs, resolve, merge, validate.

    Parameters
    ----------
    workspace_name:
        Name of the workspace to compile (must exist in specs).
    specs_dir:
        Directory containing YAML spec files.
    available_plugins:
        Known installed plugin IDs for validation.
    strict:
        If True, raise on validation errors. If False, log warnings.

    Returns
    -------
    CompiledWorkspace
        A frozen, runtime-ready workspace object.

    Raises
    ------
    CompileError
        If loading, resolution, or validation fails.
    """
    # Phase 1: Load
    registry = load_specs_dir(specs_dir)

    workspace_spec = registry.get_workspace(workspace_name)
    if workspace_spec is None:
        raise ResolutionError(
            f"Workspace '{workspace_name}' not found in {specs_dir}",
            source=str(specs_dir),
        )

    return compile_workspace_from_registry(
        workspace_spec,
        registry,
        available_plugins=available_plugins,
        strict=strict,
    )


def compile_workspace_from_registry(
    workspace_spec: WorkspaceSpec,
    registry: SpecRegistry,
    *,
    available_plugins: frozenset[str] | None = None,
    strict: bool = True,
) -> CompiledWorkspace:
    """Compile a workspace spec using an already-loaded registry.

    This is the core compile function. Use :func:`compile_workspace_from_dir`
    for the full load-from-disk pipeline.
    """
    ws_name = workspace_spec.metadata.name

    # Phase 2: Resolve references
    resolved_policies = resolve_workspace_policies(workspace_spec, registry)

    # Phase 3: Merge and compile
    compiled_policies = [compile_policy(p) for p in resolved_policies]

    ws_include = workspace_spec.spec.plugins.include
    ws_exclude = workspace_spec.spec.plugins.exclude

    compiled_agents: list[CompiledAgent] = []
    for agent_ref in workspace_spec.spec.agents:
        # Resolve template
        raw_template = resolve_workspace_agent_template(
            agent_ref, registry, ws_name,
        )

        # Resolve inheritance chain
        chain = resolve_template_chain(raw_template, registry)

        # Merge chain
        merged = merge_template_chain(chain)

        # Apply agent-level overrides
        effective = apply_agent_overrides(merged, agent_ref)

        # Compile into frozen agent
        compiled = compile_agent(
            effective,
            agent_ref,
            compiled_policies,
            (ws_include, ws_exclude),
        )
        compiled_agents.append(compiled)

    memory = compile_memory(workspace_spec)
    config = compile_workspace_config(workspace_spec)

    workspace = CompiledWorkspace(
        name=ws_name,
        agents=tuple(compiled_agents),
        policies=tuple(compiled_policies),
        memory=memory,
        plugin_include=frozenset(ws_include),
        plugin_exclude=frozenset(ws_exclude),
        config=config,
        startup_agents=tuple(workspace_spec.spec.startup.start_agents),
        preload_plugins=workspace_spec.spec.startup.preload_plugins,
    )

    # Phase 4: Validate
    errors = validate_workspace(
        workspace, available_plugins=available_plugins,
    )
    if errors:
        if strict:
            messages = [str(e) for e in errors]
            raise SpecValidationError(
                f"Workspace '{ws_name}' has {len(errors)} validation error(s):\n"
                + "\n".join(f"  - {m}" for m in messages),
                source=ws_name,
            )
        for err in errors:
            logger.warning("Validation warning: %s", err)

    return workspace


def synthesize_implicit_workspace(
    *,
    name: str = "default",
    provider: str = "copilot",
    model_id: str | None = None,
    system_prompt: str = "",
    plugins: list[str] | None = None,
) -> WorkspaceSpec:
    """Create an implicit workspace from CLI args (no YAML file needed).

    This is the fallback when no workspace.yml is specified — ensures
    users can still run single agents without writing workspace files.
    """
    agent_ref = WorkspaceAgentRef(
        name="default",
        template="implicit",
        mode="task",
    )

    return WorkspaceSpec(
        api_version="obscura/v1",
        kind="Workspace",
        metadata=SpecMetadata(
            name=name,
            description="Implicit workspace from CLI args",
        ),
        spec=WorkspaceSpecBody(
            agents=[agent_ref],
        ),
    )


def compile_workspace(
    name: str,
    *,
    available_plugins: frozenset[str] | None = None,
    strict: bool = True,
) -> CompiledWorkspace:
    """Compile a workspace from all ``.obscura/specs/`` directories.

    Merges global and local specs directories so that global templates
    and policies are available everywhere, and local specs can override.
    Falls back to the single active specs dir if no merge dirs found.
    """
    from obscura.core.paths import resolve_all_specs_dirs

    dirs = resolve_all_specs_dirs()
    if not dirs:
        # Fallback: single-dir behavior
        return compile_workspace_from_dir(
            name,
            resolve_obscura_specs_dir(),
            available_plugins=available_plugins,
            strict=strict,
        )

    from obscura.core.compiler.loader import load_specs_dirs

    registry = load_specs_dirs(dirs)
    workspace_spec = registry.get_workspace(name)
    if workspace_spec is None:
        raise ResolutionError(
            f"Workspace '{name}' not found in specs directories: {dirs}",
            source=str(dirs),
        )

    return compile_workspace_from_registry(
        workspace_spec,
        registry,
        available_plugins=available_plugins,
        strict=strict,
    )
