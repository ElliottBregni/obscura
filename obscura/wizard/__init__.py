"""obscura.wizard — config wizard surface (TUI + API + MCP).

The wizard manages user-facing configuration in ``~/.obscura/config.toml``:
prompt profiles, default backend, capability grants, plugin toggles, MCP
server bindings, agent selections, and per-workspace profile overrides.

Three frontends consume the same :class:`WizardService`:

* TUI — :mod:`obscura.wizard.tui` (run via ``obscura wizard`` or
  ``python -m obscura.wizard``).
* HTTP API — :mod:`obscura.routes.wizard` (mounted at ``/api/v1/wizard``).
* MCP tools — defined in :mod:`obscura.mcp_server.server`, prefixed
  ``wizard_*``.

The service layer is pure: no I/O beyond the config dir, no FastAPI, no
prompt-toolkit. That is what makes it consumable from every surface.
"""

from __future__ import annotations

from obscura.wizard.schema import (
    ActiveState,
    Profile,
    WizardSnapshot,
    WorkspaceBinding,
)
from obscura.wizard.service import WizardService, default_service

__all__ = [
    "ActiveState",
    "Profile",
    "WizardService",
    "WizardSnapshot",
    "WorkspaceBinding",
    "default_service",
]
