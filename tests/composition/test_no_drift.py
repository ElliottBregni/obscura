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

# Symbols whose direct use indicates a surface is bypassing composition.
# Each symbol maps to the block that owns it.
DRIFT_SYMBOLS = (
    # Plugin block (composition/blocks/plugins.py)
    "get_all_builtin_tool_specs",
    "get_filtered_builtin_tool_specs",
    "get_capability_map",
    "resolve_allowed_tools_from_config",
    # System tools block (composition/blocks/system_tools.py)
    "get_system_tool_specs",
    # Memory tools block (composition/blocks/memory_tools.py)
    "make_memory_tool_specs",
    # Browser bridge block (composition/blocks/browser_bridge.py)
    "attach_if_running",
    # Supervisor block (composition/blocks/supervisor.py)
    "AgentSupervisor",
    # KAIROS block (composition/blocks/kairos.py)
    "register_kairos_hooks",
    # UDS inbox block (composition/blocks/uds_inbox.py)
    "UDSInbox",
)

# Files that legitimately use these symbols and are NOT in violation.
# Each entry needs a one-line reason in nearby comments below.
ALLOWLIST = frozenset(
    {
        # Composition blocks themselves
        "obscura/composition/blocks/plugins.py",
        "obscura/composition/blocks/system_tools.py",
        "obscura/composition/blocks/memory_tools.py",
        "obscura/composition/blocks/browser_bridge.py",
        # /plugins slash commands inspect plugins; do not register tools
        "obscura/cli/commands.py",
        # bootstrap_all_builtins runs at workspace-init time, no AgentSession
        "obscura/core/workspace.py",
        # Plugin loader implementation + helpers
        "obscura/plugins/loader.py",
        "obscura/plugins/capabilities.py",
        "obscura/plugins/__init__.py",
        "obscura/plugins/lazy.py",
        "obscura/plugins/registries/capability_index.py",
        "obscura/plugins/registries/tool_index.py",
        # System tools module + internal helpers: define / consume the
        # canonical get_system_tool_specs aggregator
        "obscura/tools/system/__init__.py",
        "obscura/tools/system/_shared.py",
        "obscura/tools/system/_sandbox.py",
        "obscura/tools/system/_process.py",
        # Memory tools module: make_memory_tool_specs is defined here
        "obscura/tools/memory_tools.py",
        # ToolBroker provider: aggregates system tools for the broker —
        # internal aggregation mechanism, not a surface module
        "obscura/tools/providers/__init__.py",
        # KAIROS dream cycle has its own ad-hoc tool list (separate runtime);
        # tracked for future migration onto AgentSession
        "obscura/kairos/dream.py",
        # Eval framework runs prompts against a synthetic spec list to score
        # tool selection; not a surface that registers tools onto a session
        "obscura/cli/eval_commands.py",
        # Browser bridge module: attach_if_running is defined here
        "obscura/integrations/browser/client.py",
        # AgentSupervisor is defined in this module
        "obscura/agent/supervisor.py",
        # register_kairos_hooks is defined in this module
        "obscura/kairos/supervisor_hooks.py",
        # Composition supervisor + kairos blocks
        "obscura/composition/blocks/supervisor.py",
        "obscura/composition/blocks/kairos.py",
        # UDSInbox is defined here
        "obscura/kairos/uds_messaging.py",
        # Composition uds_inbox block
        "obscura/composition/blocks/uds_inbox.py",
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
