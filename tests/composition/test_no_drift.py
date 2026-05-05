"""Drift defence: legacy plugin-loading callsites must stay deleted.

The composition refactor's locked principle is "no lazy loading where
logic can drift." Once a building block (here: install_plugin_tools)
is extracted, the original callsites are deleted from the surface
modules. This test fails CI if anyone accidentally re-introduces a
direct PluginLoader / get_*_builtin_tool_specs call in a surface
module.

Allowlist:
    - composition/blocks/plugins.py — the canonical block itself
    - cli/commands.py — `/plugins` slash commands inspect plugins for
      display; they don't register tools, so they keep direct loader use
    - core/workspace.py — `bootstrap_all_builtins()` runs at workspace
      init time (before any AgentSession exists) to install plugin pip
      deps; it legitimately needs its own PluginLoader
    - plugins/loader.py and plugins/* — the loader implementation itself
    - agent/agents.py — Agent.start() builds its own resolver; will be
      migrated when the AgentRuntime/Agent path moves to compositions
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Symbols whose direct use indicates a surface is bypassing composition
DRIFT_SYMBOLS = (
    "get_all_builtin_tool_specs",
    "get_filtered_builtin_tool_specs",
    "get_capability_map",
    "resolve_allowed_tools_from_config",
)

# Files that legitimately use these symbols and are NOT in violation
ALLOWLIST = frozenset(
    {
        "obscura/composition/blocks/plugins.py",
        "obscura/cli/commands.py",
        "obscura/core/workspace.py",
        "obscura/plugins/loader.py",
        "obscura/plugins/capabilities.py",
        "obscura/plugins/__init__.py",
        "obscura/plugins/lazy.py",
        "obscura/plugins/registries/capability_index.py",
        "obscura/plugins/registries/tool_index.py",
        "obscura/agent/agents.py",  # to be migrated; tracked
    },
)


def _grep_repo(symbol: str) -> list[str]:
    """Return file:line entries from a ripgrep scan for `symbol`."""
    try:
        out = subprocess.run(
            [
                "rg",
                "-n",
                "--no-heading",
                "--type",
                "py",
                "-g",
                "!build/**",
                "-g",
                "!**/build/**",
                "-g",
                "!tests/**",
                rf"\b{re.escape(symbol)}\b",
                str(REPO_ROOT / "obscura"),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:  # pragma: no cover
        pytest.skip("ripgrep (rg) not available")
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


@pytest.mark.unit
@pytest.mark.parametrize("symbol", DRIFT_SYMBOLS)
def test_no_unsanctioned_callsites(symbol: str) -> None:
    """No file outside ALLOWLIST may directly use the symbol."""
    hits = _grep_repo(symbol)
    violations: list[str] = []
    for hit in hits:
        # hit format: /abs/path:linenum:matched_text
        path_str = hit.split(":", 1)[0]
        try:
            rel = Path(path_str).resolve().relative_to(REPO_ROOT)
        except ValueError:
            continue
        rel_str = str(rel).replace("\\", "/")
        if rel_str in ALLOWLIST:
            continue
        violations.append(hit)

    assert not violations, (
        f"Unsanctioned use of `{symbol}` outside composition. The "
        f"composition refactor extracted plugin loading into "
        f"obscura.composition.blocks.plugins.install_plugin_tools — "
        f"surface modules must call composition, not the loader "
        f"directly. If your callsite is legitimate (inspection only, "
        f"not tool registration), add it to ALLOWLIST in this test "
        f"with a comment explaining why.\n\nViolations:\n  "
        + "\n  ".join(violations)
    )
