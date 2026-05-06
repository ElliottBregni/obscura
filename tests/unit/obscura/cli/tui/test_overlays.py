"""Tests for the modal Float overlays in ``obscura.cli.tui.overlays``."""

from __future__ import annotations

import asyncio

import pytest
from prompt_toolkit.key_binding import KeyBindings

from obscura.cli.tui.overlays import (
    ApprovalAction,
    build_overlays,
)
from obscura.cli.tui.state import (
    HUDState,
    ToolApprovalRequest,
    TUIState,
)

pytestmark = pytest.mark.unit


def _make_state() -> TUIState:
    hud = HUDState(backend="copilot", model="gpt-4", session_id="abcd1234efgh")
    return TUIState(hud=hud)


def test_build_overlays_returns_four_floats() -> None:
    state = _make_state()
    overlays = build_overlays(state, command_names=lambda: ["help", "quit"])
    floats = overlays.floats()
    assert len(floats) == 4


def test_tool_approval_visibility_reflects_pending_state() -> None:
    state = _make_state()
    overlays = build_overlays(state, command_names=lambda: [])
    assert overlays.tool_approval.visible is False

    state.pending_approval = ToolApprovalRequest(
        tool_use_id="tu1",
        tool_name="bash",
        tool_input={"command": "ls"},
    )
    assert overlays.tool_approval.visible is True


def test_each_overlay_exposes_keybindings() -> None:
    state = _make_state()
    overlays = build_overlays(state, command_names=lambda: ["help"])
    for kb in overlays.all_key_bindings():
        assert isinstance(kb, KeyBindings)


async def test_tool_approval_request_resolves_via_future() -> None:
    state = _make_state()
    overlays = build_overlays(state, command_names=lambda: [])
    overlay = overlays.tool_approval

    req = ToolApprovalRequest(
        tool_use_id="tu1",
        tool_name="bash",
        tool_input={"command": "rm -rf /"},
    )

    async def resolver() -> None:
        # Yield once so request() has a chance to install the future.
        await asyncio.sleep(0)
        # Simulate the user pressing 'a' (always_allow). The overlay's
        # _resolve helper is the same path the keybinding takes.
        overlay._resolve("always_allow")  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001

    resolver_task = asyncio.create_task(resolver())
    try:
        result = await asyncio.wait_for(overlay.request(req), timeout=2.0)
    finally:
        await resolver_task

    assert isinstance(result, ApprovalAction)
    assert result.decision == "always_allow"
    # State is cleared on exit so the overlay returns to invisible.
    assert state.pending_approval is None
    assert overlay.visible is False


async def test_command_palette_request_resolves_to_chosen_command() -> None:
    state = _make_state()
    overlays = build_overlays(state, command_names=lambda: ["help", "quit"])
    palette = overlays.command_palette

    async def resolver() -> None:
        await asyncio.sleep(0)
        palette._resolve("help")  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001

    resolver_task = asyncio.create_task(resolver())
    try:
        result = await asyncio.wait_for(palette.request(), timeout=2.0)
    finally:
        await resolver_task

    assert result == "help"
    assert palette.visible is False


async def test_ask_user_overlay_returns_typed_text() -> None:
    state = _make_state()
    overlays = build_overlays(state, command_names=lambda: [])
    ask = overlays.ask_user

    async def resolver() -> None:
        await asyncio.sleep(0)
        ask._resolve("yes please")  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001

    resolver_task = asyncio.create_task(resolver())
    try:
        result = await asyncio.wait_for(ask.request("Continue?"), timeout=2.0)
    finally:
        await resolver_task

    assert result == "yes please"
    assert ask.visible is False
