"""obscura.cli.tui.layout — full-screen :mod:`prompt_toolkit` layout.

Opencode-style three-row shell: status header, scrolling transcript,
input + hint footer. Banner and notifications appear as conditional
floats (top-anchored) so they never push the input around. Modal
overlays attach as centered :class:`Float`s on the
:class:`FloatContainer` root.

Top-to-bottom shape (ALL widgets pinned by exact :class:`Dimension` so
no row gets unintended weight)::

    ┌─────────────────────────────────────────────┐
    │ header_window (1 row, exact)                │
    ├─────────────────────────────────────────────┤
    │ transcript_window (weight=1, scrolls)       │
    │                                             │
    ├─────────────────────────────────────────────┤
    │ live_region_window (cond, 1 row when active)│
    ├─────────────────────────────────────────────┤
    │ input_area (1..6 rows, multiline)           │
    ├─────────────────────────────────────────────┤
    │ toolbar_window (1 row, exact)               │
    └─────────────────────────────────────────────┘

Banner / notification stacks float on top of the body via
:class:`Float`s configured with ``top``/``right`` anchors so they don't
displace the input.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.widgets import TextArea

from obscura.cli.promptkit.highlighter import KeywordHighlighter
from obscura.cli.tui.buffers import (
    agent_panel_text,
    banner_text,
    header_text,
    live_region_text,
    notification_stack_text,
    toolbar_text,
    transcript_text,
)
from obscura.cli.tui.state import LiveRegionKind, TUIState

__all__ = [
    "TUILayoutComponents",
    "build_layout",
]


# ---------------------------------------------------------------------------
# Components dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TUILayoutComponents:
    """Handles to every live container/control built by :func:`build_layout`.

    The :class:`obscura.cli.tui.app.ObscuraTUIApp` consumes these to wire
    keybindings, focus changes, and overlay floats. The dataclass is
    frozen because callers should not swap components after construction
    — they should mutate the underlying widgets instead (e.g. append to
    ``floats_container.floats``).
    """

    layout: Layout
    input_buffer: Buffer
    input_window: Window
    transcript_window: Window
    live_region_window: Window
    notification_window: Window
    banner_window: Window
    header_window: Window
    toolbar_window: Window
    agent_panel_window: Window
    floats_container: FloatContainer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_text_window(
    get_text: Callable[[], FormattedText],
    *,
    height: Dimension | int | None = None,
    style: str = "",
    always_hide_cursor: bool = True,
) -> Window:
    """Build a :class:`Window` whose content is recomputed every frame.

    ``get_text`` is wrapped in a :class:`FormattedTextControl` so
    prompt-toolkit re-invokes it on each redraw; this is what lets
    :class:`TUIState` mutations show up live.
    """
    control = FormattedTextControl(
        text=get_text,
        focusable=False,
        show_cursor=False,
    )
    return Window(
        content=control,
        height=height,
        style=style,
        always_hide_cursor=always_hide_cursor,
        wrap_lines=True,
    )


def _scroll_to_bottom(window: Window) -> int:
    """Compute a vertical scroll that pins the window at its last line.

    Plumbed into :class:`Window`'s ``get_vertical_scroll`` so the
    transcript pane auto-scrolls as new entries land. We want the
    bottom-most line visible — so we return ``max(0, content_height -
    window_height)``.
    """
    info = window.render_info
    if info is None:
        return 0
    return max(0, info.content_height - info.window_height)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_layout(
    state: TUIState,
    *,
    completer: Completer | None = None,
    on_submit: Callable[[str], None] | None = None,
) -> TUILayoutComponents:
    """Build the full-screen :class:`Layout` reading from ``state``.

    Parameters
    ----------
    state:
        The single source of truth. Each window's text-getter closes
        over this reference, so prompt-toolkit re-reads live state on
        every frame (no snapshotting).
    completer:
        Optional :class:`prompt_toolkit.completion.Completer` (typically
        a :class:`obscura.cli.promptkit.completer.SlashCommandCompleter`)
        attached to the input :class:`TextArea`.
    on_submit:
        Called with the submitted text when the user presses Enter.
        The caller (the :class:`obscura.cli.tui.app.ObscuraTUIApp`) is
        responsible for scheduling the agent loop. Returning ``False``
        from the underlying accept handler clears the input buffer; we
        always clear after submission.

    Returns
    -------
    TUILayoutComponents
        Handles to every live container so the calling app can attach
        keybindings, swap focus, and inject overlay floats.
    """

    # ---- Header (top, 1 row) ---------------------------------------------
    header_window = _make_text_window(
        lambda: header_text(state),
        height=Dimension.exact(1),
        style="class:tui.header",
    )

    # ---- Transcript (weight=1, auto-scroll) ------------------------------
    # Anchor cursor at the LAST rendered line so prompt-toolkit's render
    # loop never indexes past content. An out-of-range cursor y crashes
    # with ``IndexError: list index out of range`` from
    # ``fragment_lines[lineno]`` in controls.py.
    def _last_line_index() -> int:
        # FormattedText's element shape is OneStyleAndTextTuple — at
        # runtime always a 2- or 3-tuple where index 1 is the text.
        # Pyright's stubs widen it past simple unpacking, so iterate
        # by index instead and let pyright stay happy.
        nl = 0
        for tup in transcript_text(state):
            text = tup[1] if len(tup) > 1 else ""
            # ``text`` is typed as ``str`` here but
            # OneStyleAndTextTuple is a positional alias whose middle
            # field can technically be a callable in some
            # prompt-toolkit versions; the guard keeps us safe.
            if isinstance(text, str):  # pyright: ignore[reportUnnecessaryIsInstance]
                nl += text.count("\n")
        return max(0, nl)

    transcript_control = FormattedTextControl(
        text=lambda: transcript_text(state),
        focusable=False,
        show_cursor=False,
        get_cursor_position=lambda: Point(x=0, y=_last_line_index()),
    )
    transcript_window = Window(
        content=transcript_control,
        wrap_lines=True,
        always_hide_cursor=True,
        height=Dimension(weight=1, min=1),
        get_vertical_scroll=_scroll_to_bottom,
        allow_scroll_beyond_bottom=False,
        style="class:tui.transcript",
    )

    # ---- Agent side panel (collapsible, right column) --------------------
    # 26 columns is the smallest comfortable width for "● agent_name\n
    # waiting · 1m23s · #5\n  ↳ tool_name" without wrapping. Toggled by
    # ``state.show_agent_panel`` (Ctrl+G in app keybindings); when off,
    # the :class:`ConditionalContainer` collapses to zero width and the
    # transcript reclaims the column.
    agent_panel_window = _make_text_window(
        lambda: agent_panel_text(state),
        style="class:tui.agent-panel",
    )
    agent_panel_container = ConditionalContainer(
        content=Window(
            content=FormattedTextControl(
                text=lambda: agent_panel_text(state),
                focusable=False,
                show_cursor=False,
            ),
            wrap_lines=False,
            always_hide_cursor=True,
            width=Dimension.exact(26),
            style="class:tui.agent-panel",
        ),
        filter=Condition(lambda: state.show_agent_panel),
    )
    transcript_row = VSplit(
        [
            transcript_window,
            agent_panel_container,
        ],
    )

    # ---- Live region (1 row, conditional) --------------------------------
    live_region_window = _make_text_window(
        lambda: live_region_text(state),
        height=Dimension.exact(1),
        style="class:tui.live",
    )
    live_region_container = ConditionalContainer(
        content=live_region_window,
        filter=Condition(lambda: state.live.kind != LiveRegionKind.IDLE),
    )

    # ---- Input area (1..6 rows, multiline) -------------------------------
    # TUI-specific input keybindings — distinct from the shared
    # ``promptkit.keybindings`` used by the bordered REPL because the
    # full-screen input needs Enter→submit semantics:
    #
    #   * Plain ``Enter`` calls ``buffer.validate_and_handle()`` which
    #     fires the accept handler. Without this binding,
    #     ``multiline=True`` makes prompt-toolkit insert a newline on
    #     Enter and the user can never submit (this was the
    #     ``yooo``-stuck-in-buffer bug).
    #   * ``Esc+Enter`` inserts a literal newline for multi-line prompts.
    #   * ``Ctrl+J`` also inserts a newline (Linux-friendly alias).
    key_bindings = KeyBindings()

    @key_bindings.add("enter")
    def _submit(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
        event.current_buffer.validate_and_handle()

    @key_bindings.add("escape", "enter")
    def _newline_esc(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
        event.current_buffer.insert_text("\n")

    @key_bindings.add("c-j")
    def _newline_ctrl_j(event: KeyPressEvent) -> None:  # pyright: ignore[reportUnusedFunction]
        event.current_buffer.insert_text("\n")

    def _accept(buffer: Buffer) -> bool:
        """Buffer accept handler — fires on plain Enter via the binding above.

        Returning ``False`` clears the buffer; returning ``True`` keeps
        the text. We always want a fresh prompt on submit.
        """
        text = buffer.text
        if on_submit is not None and text.strip():
            on_submit(text)
        return False

    input_area = TextArea(
        multiline=True,
        wrap_lines=True,
        prompt="❯ ",  # noqa: RUF001
        completer=completer,
        # ``complete_while_typing`` makes the completion menu pop up
        # as the user types ``/``, ``@``, or ``$``. The TUI app caches
        # the supplier results for ~5s so the per-keystroke filesystem
        # walk doesn't show up as input lag.
        complete_while_typing=True,
        height=Dimension(min=1, max=6, preferred=1),
        accept_handler=_accept,
        focusable=True,
        focus_on_click=True,
        input_processors=[KeywordHighlighter()],
        style="class:tui.input",
    )
    input_area.control.key_bindings = key_bindings
    input_window: Window = input_area.window
    input_buffer: Buffer = input_area.buffer

    # ---- Toolbar (1 row, exact) ------------------------------------------
    toolbar_window = _make_text_window(
        lambda: toolbar_text(state),
        height=Dimension.exact(1),
        style="class:tui.toolbar",
    )

    # ---- Banner / notification windows kept for return value -------------
    # These are NOT in the main HSplit anymore — they show up as floats
    # (banner: top-anchored; notifications: top-right toast stack) so
    # they don't displace the input.
    banner_window = _make_text_window(
        lambda: banner_text(state),
        height=Dimension.exact(1),
        style="class:tui.banner",
    )
    notification_window = _make_text_window(
        lambda: notification_stack_text(state),
        height=Dimension(min=1, max=8),
        style="class:tui.notifications",
    )

    # ---- Compose ---------------------------------------------------------
    # The live region (tool spinner / streaming label) sits BELOW the input
    # so "running shell_exec..." appears under the prompt the user is
    # interacting with rather than above it. Toolbar stays at the very
    # bottom.
    body = HSplit(
        [
            header_window,
            transcript_row,
            input_area,
            live_region_container,
            toolbar_window,
        ],
    )

    # Banner and notification stacks as conditional floats — top-anchored
    # so they appear above the transcript without pushing the input.
    banner_float = Float(
        content=ConditionalContainer(
            content=banner_window,
            filter=Condition(lambda: state.banner is not None),
        ),
        top=1,
        left=0,
        right=0,
    )
    notification_float = Float(
        content=ConditionalContainer(
            content=notification_window,
            filter=Condition(lambda: bool(state.notifications)),
        ),
        top=2,
        right=2,
        width=48,
    )

    # Completions popup — anchored to the cursor so it appears next to
    # whatever ``@command`` / ``$skill`` / ``/slash`` token the user is
    # typing. ``CompletionsMenu`` uses prompt-toolkit's built-in widget
    # which already handles up/down navigation and Enter / Tab to
    # accept; we don't need our own keybindings for it.
    completions_float = Float(
        xcursor=True,
        ycursor=True,
        content=CompletionsMenu(max_height=10, scroll_offset=1),
    )

    floats_container = FloatContainer(
        content=body,
        floats=[banner_float, notification_float, completions_float],
    )

    layout = Layout(floats_container, focused_element=input_window)

    return TUILayoutComponents(
        layout=layout,
        input_buffer=input_buffer,
        input_window=input_window,
        transcript_window=transcript_window,
        live_region_window=live_region_window,
        notification_window=notification_window,
        banner_window=banner_window,
        header_window=header_window,
        toolbar_window=toolbar_window,
        agent_panel_window=agent_panel_window,
        floats_container=floats_container,
    )
