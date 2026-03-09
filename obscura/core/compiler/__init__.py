"""obscura.core.compiler — Declarative spec compiler for Obscura.

Compiles YAML specs (workspace, template, agent, policy) into frozen
runtime objects through a deterministic pipeline:

    load → resolve → merge → validate → compiled output

Usage::

    from obscura.core.compiler import compile_workspace_from_dir, CompiledWorkspace

    workspace: CompiledWorkspace = compile_workspace_from_dir(
        workspace_name="code-mode",
        specs_dir=Path(".obscura/specs"),
    )
"""

from __future__ import annotations

from obscura.core.compiler.compile import (
    compile_workspace,
    compile_workspace_from_dir,
    compile_workspace_from_registry,
    synthesize_implicit_workspace,
)
from obscura.core.compiler.compiled import (
    CompiledAgent,
    CompiledMCPServer,
    CompiledMemory,
    CompiledPolicy,
    CompiledWorkspace,
)
from obscura.core.compiler.errors import (
    CompileError,
    MergeError,
    PluginFilterError,
    ResolutionError,
    SpecLoadError,
    SpecValidationError,
)
from obscura.core.compiler.loader import SpecRegistry, load_spec_file, load_specs_dir, load_specs_dirs
from obscura.core.compiler.validator import VALID_AGENT_TYPES
from obscura.core.compiler.migrate import MigrationResult, migrate_agents_yaml
from obscura.core.compiler.specs import (
    AgentInstanceSpec,
    AnySpec,
    MCPServerSpec,
    MemoryBindingSpec,
    PluginFilterSpec,
    PolicySpec,
    SpecMetadata,
    StartupSpec,
    TemplateSpec,
    WorkspaceAgentRef,
    WorkspaceSpec,
)

__all__ = [
    # Compile functions
    "compile_workspace",
    "compile_workspace_from_dir",
    "compile_workspace_from_registry",
    "synthesize_implicit_workspace",
    # Compiled output
    "CompiledAgent",
    "CompiledMCPServer",
    "CompiledMemory",
    "CompiledPolicy",
    "CompiledWorkspace",
    # Errors
    "CompileError",
    "MergeError",
    "PluginFilterError",
    "ResolutionError",
    "SpecLoadError",
    "SpecValidationError",
    # Loader
    "SpecRegistry",
    "load_spec_file",
    "load_specs_dir",
    "load_specs_dirs",
    # Validator
    "VALID_AGENT_TYPES",
    # Migration
    "MigrationResult",
    "migrate_agents_yaml",
    # Specs
    "AgentInstanceSpec",
    "AnySpec",
    "MCPServerSpec",
    "MemoryBindingSpec",
    "PluginFilterSpec",
    "PolicySpec",
    "SpecMetadata",
    "StartupSpec",
    "TemplateSpec",
    "WorkspaceAgentRef",
    "WorkspaceSpec",
]
