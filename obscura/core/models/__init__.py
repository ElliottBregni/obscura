"""Central Pydantic model registry for the obscura runtime.

Domain teams populate this package with frozen Pydantic models composed via
the mixins in `_mixins`. Round 2 (Team Configs) lands the typed shapes for
internal configuration dicts: ``AgentConfig``, ``MCPServerSpec``,
``HookContext``, ``BashClassification``, ``PluginManifest``, plus the
``Trigger`` discriminated union.
"""

from __future__ import annotations

from obscura.core.models._base import (
    BoundaryModel,
    MutableObscuraModel,
    ObscuraModel,
)
from obscura.core.models.configs import (
    AgentConfig,
    BashClassification,
    HookContext,
    MCPServerSpec,
    PluginManifest,
)
from obscura.core.models.triggers import (
    EmailTrigger,
    IMessageTrigger,
    MessageTrigger,
    StopTrigger,
    Trigger,
)

__all__ = [
    "AgentConfig",
    "BashClassification",
    "BoundaryModel",
    "EmailTrigger",
    "HookContext",
    "IMessageTrigger",
    "MCPServerSpec",
    "MessageTrigger",
    "MutableObscuraModel",
    "ObscuraModel",
    "PluginManifest",
    "StopTrigger",
    "Trigger",
]
