"""Tests for the modal Float overlays in ``obscura.cli.tui.overlays``."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.output import DummyOutput

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


def test_command_palette_renders_slash_prefixed_items() -> None:
    state = _make_state()
    overlays = build_overlays(state, command_names=lambda: ["help", "quit"])
    palette = overlays.command_palette
    palette.open()
    rendered = "".join(text for _, text in palette._render_list())  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    assert "/help" in rendered
    assert "Enter run" in rendered


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


def test_tool_approval_overlay_renders_summary_and_scope() -> None:
    state = _make_state()
    overlays = build_overlays(state, command_names=lambda: [])
    state.pending_approval = ToolApprovalRequest(
        tool_use_id="tu1",
        tool_name="run_command",
        tool_input={"command": "git status"},
    )
    rendered = "".join(
        text for _, text in overlays.tool_approval._render_text()  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001
    )
    assert "action:" in rendered
    assert "scope:" in rendered
    assert "git status" in rendered


async def test_plan_approval_resolves_true_on_y_keypress() -> None:
    """Regression: merge_key_bindings must wire overlay bindings into the
    Application or pressing y/n/Enter/Esc is silently ignored.

    This test builds a minimal Application with the same
    ``merge_key_bindings([app_kb] + overlays.all_key_bindings())`` pattern
    that ``ObscuraTUIApp._build_application()`` uses, then feeds a ``y``
    key through the live event loop and confirms the plan-approval Future
    resolves to ``True``.
    """
    state = _make_state()
    overlays = build_overlays(state, command_names=lambda: [])

    app_kb = KeyBindings()
    merged = merge_key_bindings([app_kb, *overlays.all_key_bindings()])

    with create_pipe_input() as pipe_input:
        app = Application(
            layout=Layout(Window()),
            key_bindings=merged,
            input=pipe_input,
            output=DummyOutput(),
        )

        async def feed_approve() -> None:
            # Wait for app.run_async() to enter its loop, then start the
            # overlay request (which sets state.banner), inject 'y', and
            # exit the app.
            await asyncio.sleep(0.05)
            approval_fut = asyncio.ensure_future(
                overlays.plan_approval.request("my plan summary")
            )
            await asyncio.sleep(0.02)
            assert overlays.plan_approval.visible, (
                "overlay should be visible after request()"
            )
            pipe_input.send_text("y")
            result = await asyncio.wait_for(approval_fut, timeout=2.0)
            assert result is True
            app.exit()

        feeder = asyncio.create_task(feed_approve())
        try:
            await asyncio.wait_for(app.run_async(), timeout=5.0)
        finally:
            feeder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await feeder


async def test_plan_approval_resolves_true_on_enter_keypress() -> None:
    """Enter (added alongside y) also approves the plan."""
    state = _make_state()
    overlays = build_overlays(state, command_names=lambda: [])

    app_kb = KeyBindings()
    merged = merge_key_bindings([app_kb, *overlays.all_key_bindings()])

    with create_pipe_input() as pipe_input:
        app = Application(
            layout=Layout(Window()),
            key_bindings=merged,
            input=pipe_input,
            output=DummyOutput(),
        )

        async def feed_enter() -> None:
            await asyncio.sleep(0.05)
            approval_fut = asyncio.ensure_future(
                overlays.plan_approval.request("enter test")
            )
            await asyncio.sleep(0.02)
            pipe_input.send_text("\r")  # Enter
            result = await asyncio.wait_for(approval_fut, timeout=2.0)
            assert result is True
            app.exit()

        feeder = asyncio.create_task(feed_enter())
        try:
            await asyncio.wait_for(app.run_async(), timeout=5.0)
        finally:
            feeder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await feeder


async def test_plan_approval_resolves_false_on_n_keypress() -> None:
    """n rejects the plan."""
    state = _make_state()
    overlays = build_overlays(state, command_names=lambda: [])

    app_kb = KeyBindings()
    merged = merge_key_bindings([app_kb, *overlays.all_key_bindings()])

    with create_pipe_input() as pipe_input:
        app = Application(
            layout=Layout(Window()),
            key_bindings=merged,
            input=pipe_input,
            output=DummyOutput(),
        )

        async def feed_reject() -> None:
            await asyncio.sleep(0.05)
            approval_fut = asyncio.ensure_future(
                overlays.plan_approval.request("reject test")
            )
            await asyncio.sleep(0.02)
            pipe_input.send_text("n")
            result = await asyncio.wait_for(approval_fut, timeout=2.0)
            assert result is False
            app.exit()

        feeder = asyncio.create_task(feed_reject())
        try:
            await asyncio.wait_for(app.run_async(), timeout=5.0)
        finally:
            feeder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await feeder


async def test_tool_approval_resolves_on_y_keypress() -> None:
    """ToolApprovalOverlay y-key fires through the merged Application bindings."""
    state = _make_state()
    overlays = build_overlays(state, command_names=lambda: [])

    app_kb = KeyBindings()
    merged = merge_key_bindings([app_kb, *overlays.all_key_bindings()])

    req = ToolApprovalRequest(
        tool_use_id="tu-kb-test",
        tool_name="run_shell",
        tool_input={"script": "echo hi"},
    )

    with create_pipe_input() as pipe_input:
        app = Application(
            layout=Layout(Window()),
            key_bindings=merged,
            input=pipe_input,
            output=DummyOutput(),
        )

        async def feed_allow() -> None:
            await asyncio.sleep(0.05)
            approval_fut = asyncio.ensure_future(overlays.tool_approval.request(req))
            await asyncio.sleep(0.02)
            assert overlays.tool_approval.visible
            pipe_input.send_text("y")
            result = await asyncio.wait_for(approval_fut, timeout=2.0)
            assert isinstance(result, ApprovalAction)
            assert result.decision == "allow"
            app.exit()

        feeder = asyncio.create_task(feed_allow())
        try:
            await asyncio.wait_for(app.run_async(), timeout=5.0)
        finally:
            feeder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await feeder
