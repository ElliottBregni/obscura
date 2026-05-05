"""obscura.core.compiler.specs — back-compat re-export of the spec models.

The canonical spec models live in :mod:`obscura.core.models.specs` as a
discriminated Pydantic union keyed on
:class:`obscura.core.enums.tools.CompilerSpecKind`. This module preserves
the historical import paths (``from obscura.core.compiler.specs import
TemplateSpec``) used across the loader / merger / resolver / compile
pipeline.
"""

from __future__ import annotations

from obscura.core.enums.tools import CompilerSpecKind
from obscura.core.models.specs import (
    AgentInstanceSpec,
    AgentInstanceSpecBody,
    AnySpec,
    CapabilityGrantSpec,
    MCPServerSpec,
    MemoryBindingSpec,
    PackSpec,
    PackSpecBody,
    PluginFilterSpec,
    PolicySpec,
    PolicySpecBody,
    SPEC_KIND_MAP,
    Spec,
    SpecMetadata,
    StartupSpec,
    TemplateSpec,
    TemplateSpecBody,
    ToolRoutingSpec,
    WorkspaceAgentRef,
    WorkspaceSpec,
    WorkspaceSpecBody,
)

__all__ = [
    "AgentInstanceSpec",
    "AgentInstanceSpecBody",
    "AnySpec",
    "CapabilityGrantSpec",
    "CompilerSpecKind",
    "MCPServerSpec",
    "MemoryBindingSpec",
    "PackSpec",
    "PackSpecBody",
    "PluginFilterSpec",
    "PolicySpec",
    "PolicySpecBody",
    "SPEC_KIND_MAP",
    "Spec",
    "SpecMetadata",
    "StartupSpec",
    "TemplateSpec",
    "TemplateSpecBody",
    "ToolRoutingSpec",
    "WorkspaceAgentRef",
    "WorkspaceSpec",
    "WorkspaceSpecBody",
]
