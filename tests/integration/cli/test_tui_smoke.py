"""Smoke test for ``obscura tui`` — Click subcommand wiring + offline layout build.

These tests do *not* run the full prompt-toolkit Application (no terminal).
They confirm:

1. The ``obscura tui`` Click subcommand is registered and ``--help`` exits 0.
2. The layout factory + overlay builder compose without raising.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from obscura.cli.tui.layout import build_layout
from obscura.cli.tui.overlays import build_overlays
from obscura.cli.tui.state import HUDState, TUIState

pytestmark = pytest.mark.integration


def test_tui_help_exits_zero() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "obscura.cli", "tui", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, (
        f"obscura tui --help exited {proc.returncode}\n"
        f"stdout:\n{proc.stdout}\n"
        f"stderr:\n{proc.stderr}"
    )
    assert "Launch the full-screen" in proc.stdout


def test_compose_layout_and_overlays_offline() -> None:
    hud = HUDState(
        backend="copilot",
        model="gpt-4o",
        session_id="abcd1234efgh5678",
    )
    state = TUIState(hud=hud)

    components = build_layout(state)
    assert components.layout is not None
    assert components.input_buffer is not None
    assert components.transcript_window is not None
    assert components.floats_container is not None

    overlays = build_overlays(state, command_names=lambda: ["help", "quit"])
    floats = overlays.floats()
    assert len(floats) == 4

    # The layout pre-installs two top-anchored floats (banner +
    # notifications). The runtime then appends overlay floats on top.
    base = len(components.floats_container.floats)
    components.floats_container.floats.extend(floats)
    assert len(components.floats_container.floats) == base + 4
