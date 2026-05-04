"""Central Pydantic model registry for the obscura runtime.

Domain teams populate this package with frozen Pydantic models composed via
the mixins in `_mixins`. Round 2 lands two complementary tracks:

- Internal/config track (Team Configs): ``AgentConfig``, ``MCPServerSpec``,
  ``HookContext``, ``BashClassification``, ``PluginManifest``, and the
  ``Trigger`` discriminated union.
- Wire-format boundary track (Team Boundary): I/O-bound models that parse
  incoming dicts into typed objects on ingress and round-trip via
  ``model_dump`` on egress — JSON-RPC, A2A, ToolResult. Imported directly
  from their submodules (``core.models.protocol``, ``core.models.a2a``,
  ``core.models.tool_result``) to keep this surface small.
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
