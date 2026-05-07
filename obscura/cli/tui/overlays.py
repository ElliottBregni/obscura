"""obscura.cli.tui.overlays — modal Float overlays for the full-screen TUI.

The full-screen ``ObscuraTUIApp`` layout is wrapped in a
:class:`prompt_toolkit.layout.FloatContainer`. Modal interactions —
tool approvals, the command palette, one-shot ``ask_user`` prompts, and
plan-approval banners — live as :class:`Float` instances inside that
container, each guarded by a visibility :class:`Condition` reading from
:class:`obscura.cli.tui.state.TUIState`.

Each overlay exposes the same shape:

* a ``visible`` property — read by the layout's filter so the overlay
  draws only while active.
* a ``float`` property — the prompt-toolkit ``Float`` instance to slot
  into the layout's ``FloatContainer(floats=...)`` list once at startup.
* a ``request(...)`` async method — opens the overlay, awaits the user's
  decision, clears state, returns the result.

The overlays use ``asyncio.Future`` for the answer-handoff: key bindings
call ``fut.set_result(...)`` and the awaiting coroutine resumes. The
visibility flag toggles back to ``False`` once state is cleared.

No lazy imports. Every dependency is at module top.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import ConditionalContainer, Float, HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.widgets import Frame

from obscura.cli.renderer.modern.theme import (
    GREEN,
    LAVENDER,
    MAUVE,
    PEACH,
    RED,
    SUBTEXT0,
    TEXT,
)
from obscura.cli.tui.state import (
    ApprovalRisk,
    BannerState,
    ToolApprovalRequest,
    TUIState,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ApprovalAction",
    "AskUserOverlay",
    "CommandPaletteOverlay",
    "PlanApprovalOverlay",
    "ToolApprovalOverlay",
    "TUIOverlays",
    "build_overlays",
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalAction:
    """Result returned by the tool-approval modal."""

    decision: Literal["allow", "deny", "always_allow"]


# ---------------------------------------------------------------------------
# Style helpers
# ---------------------------------------------------------------------------


_RISK_BORDER_HEX: dict[ApprovalRisk, str] = {
    ApprovalRisk.LOW: GREEN.hex,
    ApprovalRisk.MEDIUM: PEACH.hex,
    ApprovalRisk.HIGH: RED.hex,
}


def _risk_style(risk: ApprovalRisk) -> str:
    """prompt-toolkit style string for the approval frame border + title."""
    return f"fg:{_RISK_BORDER_HEX[risk]} bold"


def _truncate_lines(text: str, max_lines: int) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    head = lines[: max_lines - 1]
    head.append(f"… ({len(lines) - (max_lines - 1)} more lines)")
    return "\n".join(head)


def _format_args(tool_input: dict[str, object]) -> str:
    """Pretty-print tool args, capped at ~20 lines."""
    try:
        body = json.dumps(tool_input, indent=2, default=str, sort_keys=True)
    except (TypeError, ValueError):
        # Tool inputs are usually JSON-clean but custom objects can
        # slip through; the overlay falls back to ``repr`` to keep
        # the modal usable. Logged so deep logs surface the cause.
        logger.debug(
            "tui overlays: json.dumps failed for tool_input, falling back to repr",
            exc_info=True,
        )
        body = repr(tool_input)
    return _truncate_lines(body, max_lines=20)


# ---------------------------------------------------------------------------
# Tool approval overlay
# ---------------------------------------------------------------------------


class ToolApprovalOverlay:
    """Float showing a pending :class:`ToolApprovalRequest`.

    Hotkeys: ``y`` allow, ``n`` deny, ``a`` always-allow, ``Esc`` deny.
    Border colour reflects ``request.risk`` — green/peach/red.
    """

    WIDTH = 80
    HEIGHT = 18

    def __init__(self, state: TUIState) -> None:
        self._state = state
        self._fut: asyncio.Future[ApprovalAction] | None = None

        self._control = FormattedTextControl(
            text=self._render_text,
            focusable=False,
            show_cursor=False,
        )
        self._window = Window(
            content=self._control,
            wrap_lines=True,
            always_hide_cursor=True,
        )
        self._frame = Frame(
            body=self._window,
            title=self._frame_title,
            style="class:tui.overlay.tool-approval",
        )
        self._kb = self._build_keybindings()
        # Attach key bindings via a wrapper container — we re-use Frame's
        # default key handling but layer ours on top using the
        # prompt_toolkit application's global keybindings registration in
        # the runtime. We expose ``key_bindings`` for the runtime to merge.

        self._float = Float(
            content=ConditionalContainer(
                content=self._frame,
                filter=Condition(lambda: self.visible),
            ),
            width=self.WIDTH,
            height=self.HEIGHT,
        )

    # ---- properties --------------------------------------------------------

    @property
    def visible(self) -> bool:
        return self._state.pending_approval is not None

    @property
    def float(self) -> Float:
        return self._float

    @property
    def key_bindings(self) -> KeyBindings:
        """Keybindings to merge into the application; filtered to visible."""
        return self._kb

    # ---- rendering ---------------------------------------------------------

    def _frame_title(self) -> str:
        req = self._state.pending_approval
        if req is None:
            return ""
        risk_label = req.risk.value.upper()
        return f" Tool approval — {req.tool_name}  [{risk_label} risk] "

    def _render_text(self) -> FormattedText:
        req = self._state.pending_approval
        if req is None:
            return FormattedText([])

        border = _risk_style(req.risk)
        body: list[tuple[str, str]] = []
        body.append((f"fg:{TEXT.hex} bold", f"{req.tool_name}\n"))
        body.append(("", "\n"))
        args = _format_args(req.tool_input)
        body.append((f"fg:{SUBTEXT0.hex}", "args:\n"))
        body.append((f"fg:{TEXT.hex}", args + "\n"))
        if req.preview:
            body.append(("", "\n"))
            body.append((f"fg:{SUBTEXT0.hex}", "preview:\n"))
            body.append((f"fg:{TEXT.hex}", _truncate_lines(req.preview, 8) + "\n"))
        body.append(("", "\n"))
        body.append(
            (
                border,
                "[y] allow   [n] deny   [a] always allow   [Esc] cancel",
            )
        )
        return FormattedText(body)

    # ---- key bindings ------------------------------------------------------

    def _build_keybindings(self) -> KeyBindings:
        kb = KeyBindings()
        cond = Condition(lambda: self.visible)

        @kb.add("y", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._resolve("allow")

        @kb.add("Y", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._resolve("allow")

        @kb.add("n", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._resolve("deny")

        @kb.add("N", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._resolve("deny")

        @kb.add("a", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._resolve("always_allow")

        @kb.add("A", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._resolve("always_allow")

        @kb.add("escape", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._resolve("deny")

        return kb

    def _resolve(self, decision: Literal["allow", "deny", "always_allow"]) -> None:
        fut = self._fut
        if fut is None or fut.done():
            return
        fut.set_result(ApprovalAction(decision=decision))

    # ---- public coroutine --------------------------------------------------

    async def request(self, req: ToolApprovalRequest) -> ApprovalAction:
        """Open the modal, wait for a decision, clear state, return result."""
        loop = asyncio.get_event_loop()
        self._fut = loop.create_future()
        self._state.pending_approval = req
        try:
            return await self._fut
        finally:
            self._state.pending_approval = None
            self._fut = None


# ---------------------------------------------------------------------------
# Command palette overlay
# ---------------------------------------------------------------------------


class CommandPaletteOverlay:
    """Ctrl-K palette — filterable list of slash commands.

    Hotkeys: Up/Down navigate, Enter selects, Esc cancels, type to filter.
    """

    WIDTH = 60
    HEIGHT = 16

    def __init__(
        self,
        state: TUIState,
        command_names: Callable[[], list[str]],
    ) -> None:
        self._state = state
        self._command_names = command_names
        self._fut: asyncio.Future[str] | None = None
        self._open = False
        self._selected = 0

        self._buffer = Buffer(
            on_text_changed=self._on_filter_changed,
            multiline=False,
        )

        self._input_window = Window(
            content=BufferControl(buffer=self._buffer),
            height=1,
            wrap_lines=False,
        )

        self._list_control = FormattedTextControl(
            text=self._render_list,
            focusable=False,
            show_cursor=False,
        )
        self._list_window = Window(
            content=self._list_control,
            wrap_lines=False,
            always_hide_cursor=True,
        )

        body = HSplit(
            [
                self._input_window,
                Window(height=1, char="─", style=f"fg:{SUBTEXT0.hex}"),
                self._list_window,
            ]
        )
        self._frame = Frame(
            body=body,
            title=" Command palette ",
            style="class:tui.overlay.palette",
        )
        self._kb = self._build_keybindings()

        self._float = Float(
            content=ConditionalContainer(
                content=self._frame,
                filter=Condition(lambda: self.visible),
            ),
            width=self.WIDTH,
            height=self.HEIGHT,
        )

    # ---- properties --------------------------------------------------------

    @property
    def visible(self) -> bool:
        return self._open

    @property
    def float(self) -> Float:
        return self._float

    @property
    def key_bindings(self) -> KeyBindings:
        return self._kb

    @property
    def buffer(self) -> Buffer:
        """Exposed so the runtime can give it focus when the palette opens."""
        return self._buffer

    # ---- filtering ---------------------------------------------------------

    def _filtered(self) -> list[str]:
        query = self._buffer.text.lower().lstrip("/")
        names = self._command_names()
        if not query:
            return list(names)
        return [n for n in names if query in n.lower()]

    def _on_filter_changed(self, _buf: Buffer) -> None:
        # Reset selection on every keystroke.
        self._selected = 0

    def _render_list(self) -> FormattedText:
        items = self._filtered()
        if not items:
            return FormattedText(
                [(f"fg:{SUBTEXT0.hex} italic", "  (no commands match)\n")]
            )

        out: list[tuple[str, str]] = []
        max_show = self.HEIGHT - 4
        idx = max(0, min(self._selected, len(items) - 1))
        # Window the list around the selected index.
        start = max(0, idx - max_show + 1)
        end = min(len(items), start + max_show)
        for i, name in enumerate(items[start:end], start=start):
            if i == idx:
                out.append((f"fg:{LAVENDER.hex} bold reverse", f"  {name}\n"))
            else:
                out.append((f"fg:{TEXT.hex}", f"  {name}\n"))
        return FormattedText(out)

    # ---- key bindings ------------------------------------------------------

    def _build_keybindings(self) -> KeyBindings:
        kb = KeyBindings()
        cond = Condition(lambda: self.visible)

        @kb.add("up", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            if self._selected > 0:
                self._selected -= 1

        @kb.add("down", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            n = len(self._filtered())
            if self._selected < n - 1:
                self._selected += 1

        @kb.add("enter", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            items = self._filtered()
            if not items:
                self._resolve("")
                return
            idx = max(0, min(self._selected, len(items) - 1))
            self._resolve(items[idx])

        @kb.add("escape", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._resolve("")

        return kb

    def _resolve(self, value: str) -> None:
        fut = self._fut
        self._open = False
        if fut is not None and not fut.done():
            fut.set_result(value)

    # ---- public ------------------------------------------------------------

    def open(self) -> None:
        self._open = True
        self._buffer.text = ""
        self._selected = 0

    def close(self) -> None:
        self._resolve("")

    async def request(self) -> str:
        """Open palette, await selection. Returns selected command (no slash)."""
        loop = asyncio.get_event_loop()
        self._fut = loop.create_future()
        self.open()
        try:
            value = await self._fut
        finally:
            self._fut = None
            self._open = False
        return value.lstrip("/") if value else ""


# ---------------------------------------------------------------------------
# Ask-user overlay
# ---------------------------------------------------------------------------


class AskUserOverlay:
    """One-shot text-input float for ``ask_user_callback`` tool calls.

    Hotkeys: Enter submits, Esc cancels (returns "").
    """

    WIDTH = 60
    HEIGHT = 5

    def __init__(self, state: TUIState) -> None:
        self._state = state
        self._fut: asyncio.Future[str] | None = None
        self._prompt = ""
        self._open = False

        self._buffer = Buffer(multiline=False)

        self._prompt_control = FormattedTextControl(
            text=self._render_prompt,
            focusable=False,
            show_cursor=False,
        )
        self._prompt_window = Window(
            content=self._prompt_control,
            height=1,
            wrap_lines=True,
        )
        self._input_window = Window(
            content=BufferControl(buffer=self._buffer),
            height=1,
            wrap_lines=False,
        )
        body = HSplit([self._prompt_window, self._input_window])
        self._frame = Frame(
            body=body,
            title=" Ask user ",
            style="class:tui.overlay.ask-user",
        )
        self._kb = self._build_keybindings()

        self._float = Float(
            content=ConditionalContainer(
                content=self._frame,
                filter=Condition(lambda: self.visible),
            ),
            width=self.WIDTH,
            height=self.HEIGHT,
        )

    # ---- properties --------------------------------------------------------

    @property
    def visible(self) -> bool:
        return self._open

    @property
    def float(self) -> Float:
        return self._float

    @property
    def key_bindings(self) -> KeyBindings:
        return self._kb

    @property
    def buffer(self) -> Buffer:
        return self._buffer

    # ---- rendering ---------------------------------------------------------

    def _render_prompt(self) -> FormattedText:
        return FormattedText(
            [(f"fg:{MAUVE.hex} bold", _truncate_lines(self._prompt, 3))]
        )

    # ---- key bindings ------------------------------------------------------

    def _build_keybindings(self) -> KeyBindings:
        kb = KeyBindings()
        cond = Condition(lambda: self.visible)

        @kb.add("enter", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._resolve(self._buffer.text)

        @kb.add("escape", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._resolve("")

        return kb

    def _resolve(self, value: str) -> None:
        fut = self._fut
        self._open = False
        if fut is not None and not fut.done():
            fut.set_result(value)

    # ---- public ------------------------------------------------------------

    async def request(self, prompt: str) -> str:
        loop = asyncio.get_event_loop()
        self._fut = loop.create_future()
        self._prompt = prompt
        self._buffer.text = ""
        self._open = True
        try:
            return await self._fut
        finally:
            self._fut = None
            self._open = False
            self._prompt = ""


# ---------------------------------------------------------------------------
# Plan-approval overlay (sticky banner)
# ---------------------------------------------------------------------------


class PlanApprovalOverlay:
    """Sticky plan-approval banner with [Approve] [Reject].

    Hotkeys: ``y`` approve, ``n`` reject, ``Esc`` reject.
    Visibility is driven by ``state.banner.kind == "plan_approval"``.
    """

    WIDTH = 76
    HEIGHT = 12

    def __init__(self, state: TUIState) -> None:
        self._state = state
        self._fut: asyncio.Future[bool] | None = None

        self._control = FormattedTextControl(
            text=self._render_text,
            focusable=False,
            show_cursor=False,
        )
        self._window = Window(
            content=self._control,
            wrap_lines=True,
            always_hide_cursor=True,
        )
        self._frame = Frame(
            body=self._window,
            title=" Plan approval ",
            style="class:tui.overlay.plan-approval",
        )
        self._kb = self._build_keybindings()

        self._float = Float(
            content=ConditionalContainer(
                content=self._frame,
                filter=Condition(lambda: self.visible),
            ),
            width=self.WIDTH,
            height=self.HEIGHT,
        )

    # ---- properties --------------------------------------------------------

    @property
    def visible(self) -> bool:
        b = self._state.banner
        return b is not None and b.kind == "plan_approval"

    @property
    def float(self) -> Float:
        return self._float

    @property
    def key_bindings(self) -> KeyBindings:
        return self._kb

    # ---- rendering ---------------------------------------------------------

    def _render_text(self) -> FormattedText:
        b = self._state.banner
        if b is None:
            return FormattedText([])
        out: list[tuple[str, str]] = []
        if b.title:
            out.append((f"fg:{LAVENDER.hex} bold", f"{b.title}\n\n"))
        body = _truncate_lines(b.body, 8)
        out.append((f"fg:{TEXT.hex}", body + "\n"))
        out.append(("", "\n"))
        out.append(
            (
                f"fg:{GREEN.hex} bold",
                "[y] / [Enter] approve   ",
            )
        )
        out.append(
            (
                f"fg:{RED.hex} bold",
                "[n] reject   [Esc] reject",
            )
        )
        return FormattedText(out)

    # ---- key bindings ------------------------------------------------------

    def _build_keybindings(self) -> KeyBindings:
        kb = KeyBindings()
        cond = Condition(lambda: self.visible)

        @kb.add("y", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._resolve(True)

        @kb.add("Y", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._resolve(True)

        @kb.add("n", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._resolve(False)

        @kb.add("N", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._resolve(False)

        @kb.add("escape", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._resolve(False)

        @kb.add("enter", filter=cond, eager=True)
        def _(event: KeyPressEvent) -> None:
            self._resolve(True)

        return kb

    def _resolve(self, value: bool) -> None:
        fut = self._fut
        if fut is None or fut.done():
            return
        fut.set_result(value)

    # ---- public ------------------------------------------------------------

    async def request(self, summary: str) -> bool:
        loop = asyncio.get_event_loop()
        self._fut = loop.create_future()
        self._state.banner = BannerState(
            kind="plan_approval",
            title="Plan approval",
            body=summary,
            actions=["approve", "reject"],
        )
        try:
            return await self._fut
        finally:
            # Only clear the banner if it's still our plan-approval banner.
            current = self._state.banner
            # ``state.banner`` is typed non-Optional but mutators
            # may set it to None mid-flow; the guard is defensive.
            if current is not None and current.kind == "plan_approval":  # pyright: ignore[reportUnnecessaryComparison]
                self._state.banner = None
            self._fut = None


# ---------------------------------------------------------------------------
# Aggregate handle + factory
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TUIOverlays:
    """Aggregate handle the runtime threads into engine-adapter callbacks."""

    tool_approval: ToolApprovalOverlay
    command_palette: CommandPaletteOverlay
    ask_user: AskUserOverlay
    plan_approval: PlanApprovalOverlay

    def floats(self) -> list[Float]:
        """All overlay floats in z-order — pass to ``FloatContainer(floats=)``.

        Order: ask-user, palette, plan-approval, tool-approval (last on top).
        """
        return [
            self.ask_user.float,
            self.command_palette.float,
            self.plan_approval.float,
            self.tool_approval.float,
        ]

    def all_key_bindings(self) -> list[KeyBindings]:
        """Convenience for the runtime — every overlay's KeyBindings."""
        return [
            self.tool_approval.key_bindings,
            self.command_palette.key_bindings,
            self.ask_user.key_bindings,
            self.plan_approval.key_bindings,
        ]


def build_overlays(
    state: TUIState,
    *,
    command_names: Callable[[], list[str]],
) -> TUIOverlays:
    """Factory — build all four overlays bound to ``state``."""
    return TUIOverlays(
        tool_approval=ToolApprovalOverlay(state),
        command_palette=CommandPaletteOverlay(state, command_names),
        ask_user=AskUserOverlay(state),
        plan_approval=PlanApprovalOverlay(state),
    )
