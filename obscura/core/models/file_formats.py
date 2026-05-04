"""Pydantic models for on-disk file formats consumed by the runtime.

This module owns typed shapes for files that live under ``~/.obscura/``
or ``.obscura/`` and roundtrip through TOML/JSON loaders. ``BoundaryModel``
is the right base for these because the schemas evolve and unknown keys
must not break startup on older Obscura versions reading newer files.

Each model exposes a ``to_dict()`` that matches the on-disk shape so
callers that already work with ``dict[str, Any]`` can keep their
indexing patterns (``cfg["plugins"]["load_builtins"]`` etc.) without a
public-API rewrite.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from obscura.core.models._base import BoundaryModel, MutableObscuraModel

# ---------------------------------------------------------------------------
# Workspace config (~/.obscura/config.toml or .obscura/config.toml)
# ---------------------------------------------------------------------------


class CapabilitiesSection(BoundaryModel):
    """``[defaults.capabilities]`` — capability grants and denies."""

    grant: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


class DefaultsSection(BoundaryModel):
    """``[defaults]`` — runtime-wide defaults applied to every agent."""

    capabilities: CapabilitiesSection = Field(default_factory=CapabilitiesSection)


class PluginBootstrapSection(BoundaryModel):
    """``[plugins.bootstrap]`` — pip install behaviour for plugin deps."""

    auto_install: bool = True
    lenient_builtins: bool = True


class PluginsSection(BoundaryModel):
    """``[plugins]`` — plugin discovery and bootstrap settings."""

    load_builtins: bool = True
    bootstrap: PluginBootstrapSection = Field(default_factory=PluginBootstrapSection)
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


class MCPSection(BoundaryModel):
    """``[mcp]`` — MCP discovery toggle."""

    auto_discover: bool = True


class WorkspaceConfig(BoundaryModel):
    """Typed view of ``config.toml`` (``~/.obscura/`` or ``.obscura/``).

    Boundary model: tolerates unknown top-level keys for forward-compat
    (older Obscura builds reading newer config files should not fail).
    Fields cover the shape used by ``obscura.plugins.loader``,
    ``obscura.plugins.capabilities``, and ``obscura.core.workspace``.
    """

    mode: str = "code"
    plugins: PluginsSection = Field(default_factory=PluginsSection)
    defaults: DefaultsSection = Field(default_factory=DefaultsSection)
    mcp: MCPSection = Field(default_factory=MCPSection)


# ---------------------------------------------------------------------------
# Bootstrap summary — return shape of ``bootstrap_all_builtins``
# ---------------------------------------------------------------------------


class BootstrapSummary(MutableObscuraModel):
    """Result of running plugin-bootstrap across all builtin manifests.

    Mutable because the workspace builder appends to each list as
    individual plugin installs complete. ``to_dict`` mirrors the
    historical wire shape (``{"installed": [...], "skipped": [...], ...}``)
    so existing CLI consumers indexing by string key continue to work.
    """

    installed: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "installed": list(self.installed),
            "skipped": list(self.skipped),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# External-migration decision marker (~/.obscura/state/external_migration.json)
# ---------------------------------------------------------------------------


class ExternalMigrationDecision(BoundaryModel):
    """Per-source decision recorded in the migration marker file."""

    status: str
    at: str = ""


class ExternalMigrationMarker(BoundaryModel):
    """The marker JSON envelope at ``state/external_migration.json``.

    Only the ``decisions`` map is structured here; unknown top-level
    keys (added by future schema bumps) are tolerated via
    ``BoundaryModel``'s ``extra="ignore"``.
    """

    decisions: Mapping[str, ExternalMigrationDecision] = Field(default_factory=dict)


__all__ = [
    "BootstrapSummary",
    "CapabilitiesSection",
    "DefaultsSection",
    "ExternalMigrationDecision",
    "ExternalMigrationMarker",
    "MCPSection",
    "PluginBootstrapSection",
    "PluginsSection",
    "WorkspaceConfig",
]


def workspace_default_dict() -> dict[str, Any]:
    """Return the canonical default ``WorkspaceConfig`` as a plain dict.

    Used by ``obscura.core.workspace`` to seed the merge target before
    layering on user/project ``config.toml`` files. The returned shape
    matches the historical hand-written default used before the typed
    ``WorkspaceConfig`` model existed — only the keys actually populated
    in the seed are emitted, so the merged dict callers see is
    byte-for-byte unchanged.
    """
    return {
        "plugins": {
            "load_builtins": True,
            "bootstrap": {
                "auto_install": True,
                "lenient_builtins": True,
            },
        },
        "mode": "code",
        "defaults": {
            "capabilities": {
                "grant": [
                    "shell.exec",
                    "file.read",
                    "file.write",
                    "git.ops",
                    "web.browse",
                    "search.web",
                    "security.scan",
                ],
                "deny": [],
            },
        },
        "mcp": {
            "auto_discover": True,
        },
    }
