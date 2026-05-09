"""obscura.cli.tui.buffers — pure ``FormattedText`` factories.

Each public function in this module reads a :class:`TUIState` snapshot
and returns a prompt-toolkit :class:`FormattedText` (a list of
``(style, text)`` tuples). The functions are deliberately *pure*:

* No global state. The caller passes :class:`TUIState`.
* No side effects. The functions never mutate anything.
* No prompt-toolkit ``Application`` access. They are safe to call from
  a renderer thread, from a test, or from the ``layout.py`` factory.

The functions are wired into :class:`prompt_toolkit.layout.controls.FormattedTextControl`
instances as zero-argument callables that close over the live
:class:`TUIState` reference, so prompt-toolkit can re-invoke them on
every frame.

Style strings follow prompt-toolkit conventions — class references like
``"class:tool"`` resolve against the merged style dictionary built in
:mod:`obscura.cli.promptkit.style`. Inline colour strings (``"#89b4fa
bold"``) come straight from
:mod:`obscura.cli.renderer.modern.theme`.
"""

from __future__ import annotations

from prompt_toolkit.formatted_text import FormattedText

from obscura.cli.renderer.modern.theme import (
    BLUE,
    GREEN,
    LAVENDER,
    MAUVE,
    OVERLAY0,
    OVERLAY1,
    PEACH,
    RED,
    SAPPHIRE,
    SUBTEXT0,
    TEAL,
    TEXT,
    YELLOW,
)
from obscura.cli.renderer.channels import Severity
from obscura.cli.tui.state import (
    LiveRegionKind,
    NotificationItem,
    TranscriptEntry,
    TranscriptKind,
    TUIState,
)

__all__ = [
    "agent_panel_text",
    "banner_text",
    "header_text",
    "live_region_text",
    "notification_stack_text",
    "toolbar_text",
    "transcript_text",
]

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# How many transcript entries to render at most. The state itself caps at
# 5000, but the layout window only needs the recent tail.
_TRANSCRIPT_RENDER_CAP = 1000

# Maximum chars from the live region preview before we ellipsize. The
# live region renders on a single row pinned by ``Dimension.exact(1)``
# in :mod:`obscura.cli.tui.layout`; combined with ``wrap_lines=False``
# on the live-region window this is now defence-in-depth (the window
# would clip horizontally anyway), but a tighter cap keeps the spinner
# legible on narrow terminals where the truncation point would otherwise
# fall mid-word.
_LIVE_PREVIEW_MAX = 60

# Spinner frames. We pick the frame from ``state.live.spinner_idx`` so
# the live region animates without re-rendering anything else.
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# ---------------------------------------------------------------------------
# Style helpers
# ---------------------------------------------------------------------------


def _kind_prefix(kind: TranscriptKind) -> tuple[str, str]:
    """Return ``(style, glyph)`` for the leading marker of a transcript kind."""
    match kind:
        case TranscriptKind.USER:
            return f"{LAVENDER.hex} bold", "❯ "  # noqa: RUF001
        case TranscriptKind.ASSISTANT:
            return f"{BLUE.hex} bold", "● "
        case TranscriptKind.THINKING:
            return f"{MAUVE.hex} italic", "· "
        case TranscriptKind.TOOL_USE:
            return f"{YELLOW.hex} bold", "▸ "
        case TranscriptKind.TOOL_RESULT:
            return f"{TEAL.hex}", "↳ "
        case TranscriptKind.SYSTEM:
            return f"{OVERLAY0.hex}", "• "
        case TranscriptKind.ERROR:
            return f"{RED.hex} bold", "✖ "
        case TranscriptKind.SLASH_OUTPUT:
            return f"{SAPPHIRE.hex}", "/ "
        case TranscriptKind.NOTIFICATION_LOG:
            return f"{OVERLAY1.hex}", "« "


def _severity_color(sev: Severity) -> str:
    """Map a notification severity to a prompt-toolkit hex+bold spec."""
    match sev:
        case Severity.ERROR:
            return f"{RED.hex} bold"
        case Severity.WARN:
            return f"{PEACH.hex} bold"
        case Severity.SUCCESS:
            return f"{GREEN.hex}"
        case Severity.INFO:
            return f"{BLUE.hex}"


def _live_kind_color(kind: LiveRegionKind) -> str:
    """Spinner colour for each live-region kind."""
    match kind:
        case LiveRegionKind.IDLE:
            return f"{OVERLAY0.hex}"
        case LiveRegionKind.THINKING:
            return f"{MAUVE.hex} bold"
        case LiveRegionKind.STREAMING:
            return f"{BLUE.hex} bold"
        case LiveRegionKind.TOOL_RUNNING:
            return f"{YELLOW.hex} bold"


def _truncate(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars, appending an ellipsis if cut."""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"


def _format_elapsed(seconds: float) -> str:
    """Humanised elapsed display: ``"4s"`` / ``"1m07s"`` / ``"12m"``."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m{sec:02d}s"
    h, mins = divmod(m, 60)
    return f"{h}h{mins:02d}m"


def _entry_runs(entry: TranscriptEntry) -> list[tuple[str, str]]:
    """Render a single :class:`TranscriptEntry` to (style, text) tuples.

    The entry's own :class:`StyledRun` content is emitted verbatim. A
    one-line collapsed entry shows only its first run plus a marker.
    """
    out: list[tuple[str, str]] = []
    prefix_style, prefix_glyph = _kind_prefix(entry.kind)
    out.append((prefix_style, prefix_glyph))

    if entry.collapsed and entry.runs:
        head = entry.runs[0]
        out.append((head.style, _truncate(head.text, 120)))
        out.append((f"{OVERLAY0.hex}", "  … (collapsed)"))
    else:
        for run in entry.runs:
            out.append((run.style, run.text))

    if not out or not out[-1][1].endswith("\n"):
        out.append(("", "\n"))
    return out


# ---------------------------------------------------------------------------
# Public buffer factories
# ---------------------------------------------------------------------------


def transcript_text(state: TUIState) -> FormattedText:
    """Render the entire transcript as a single :class:`FormattedText`.

    Caps to the most recent :data:`_TRANSCRIPT_RENDER_CAP` entries so a
    very long session does not blow out the renderer. Each
    :class:`TranscriptEntry`'s :class:`StyledRun`\\s are appended in
    order with a kind-specific glyph prefix.
    """
    entries = state.transcript
    if not entries:
        # Empty-state hint so the launch screen isn't a void.
        hint_style = f"fg:{OVERLAY0.hex}"
        return FormattedText(
            [
                ("", "\n"),
                (hint_style, "  Welcome to Obscura.\n"),
                ("", "\n"),
                (hint_style, "  Type a message and press Enter to send.\n"),
                (hint_style, "  Esc+Enter or Ctrl+J inserts a newline.\n"),
                (hint_style, "  /help for commands · Ctrl+C cancels · Ctrl+D exits.\n"),
            ]
        )

    # Tool-call filter: show only TOOL_USE / TOOL_RESULT entries when
    # ``state.transcript_filter == "tools_only"`` (toggled with Ctrl+T).
    # Long sessions bury tool-call lines under assistant prose; this
    # gives the user a one-keystroke "show me what the agent actually
    # ran" view without changing the underlying transcript.
    if state.transcript_filter == "tools_only":
        tool_kinds = {TranscriptKind.TOOL_USE, TranscriptKind.TOOL_RESULT}
        filtered = [e for e in entries if e.kind in tool_kinds]
        if not filtered:
            empty_style = f"fg:{OVERLAY0.hex}"
            return FormattedText(
                [
                    ("", "\n"),
                    (empty_style, "  No tool calls yet in this session.\n"),
                    (empty_style, "  Ctrl+T to show all transcript entries.\n"),
                ],
            )
        entries = filtered
    if len(entries) > _TRANSCRIPT_RENDER_CAP:
        entries = entries[-_TRANSCRIPT_RENDER_CAP:]

    runs: list[tuple[str, str]] = []
    for entry in entries:
        runs.extend(_entry_runs(entry))
    return FormattedText(runs)


def live_region_text(state: TUIState) -> FormattedText:
    """Render the one-line live region (spinner + verb + preview + timer).

    Returns an empty :class:`FormattedText` when the live region is
    :attr:`LiveRegionKind.IDLE` so the surrounding
    :class:`ConditionalContainer` can hide it cleanly.
    """
    live = state.live
    if live.kind == LiveRegionKind.IDLE:
        return FormattedText([])

    spinner_char = _SPINNER_FRAMES[live.spinner_idx % len(_SPINNER_FRAMES)]
    spinner_style = _live_kind_color(live.kind)

    runs: list[tuple[str, str]] = [
        (spinner_style, f"{spinner_char} "),
        (f"{TEXT.hex} bold", live.label or live.kind.value),
    ]
    if live.preview:
        runs.append((f"{OVERLAY0.hex} italic", "  "))
        runs.append(
            (f"{OVERLAY0.hex} italic", _truncate(live.preview, _LIVE_PREVIEW_MAX)),
        )
    runs.append(("", "  "))
    runs.append((f"{OVERLAY1.hex}", _format_elapsed(live.elapsed_s)))
    return FormattedText(runs)


def notification_stack_text(state: TUIState) -> FormattedText:
    """Render the auto-dismissing notification stack.

    Newest notifications appear at the bottom, matching the order in
    :attr:`TUIState.notifications`. Each item shows a coloured severity
    pip, the title (or source), and a truncated body line.
    """
    items: list[NotificationItem] = state.notifications
    if not items:
        return FormattedText([])

    runs: list[tuple[str, str]] = []
    for item in items:
        sev_style = _severity_color(item.severity)
        runs.append((sev_style, "● "))
        title = item.title or item.source or item.severity.value
        runs.append((f"{TEXT.hex} bold", title))
        if item.body:
            runs.append(("", "  "))
            runs.append((f"{SUBTEXT0.hex}", _truncate(item.body, 96)))
        runs.append(("", "\n"))
    # Drop the trailing newline so the surrounding window doesn't show
    # an extra blank row.
    if runs and runs[-1] == ("", "\n"):
        runs.pop()
    return FormattedText(runs)


def banner_text(state: TUIState) -> FormattedText:
    """Render the sticky banner.

    Returns empty when no banner is active. The banner shows kind glyph,
    title, and a dimmed body separated by a middle dot.
    """
    banner = state.banner
    if banner is None:
        return FormattedText([])

    kind_glyph = {
        "plan_approval": "◆",
        "capability_denial": "⛔",
        "arbiter_kill": "☠",
        "compaction": "✂",
    }.get(banner.kind, "◆")

    runs: list[tuple[str, str]] = [
        (f"{PEACH.hex} bold", f"{kind_glyph} "),
        (f"{TEXT.hex} bold", banner.title),
    ]
    if banner.body:
        runs.append((f"{OVERLAY0.hex}", "  ·  "))
        runs.append((f"{SUBTEXT0.hex}", banner.body))
    if banner.actions:
        runs.append((f"{OVERLAY0.hex}", "  ["))
        runs.append((f"{BLUE.hex}", "/".join(banner.actions)))
        runs.append((f"{OVERLAY0.hex}", "]"))
    return FormattedText(runs)


def header_text(state: TUIState) -> FormattedText:
    """Render the top-of-screen header line.

    Layout (left-to-right):

    * session title (or short session id)
    * backend / model
    * branch (when present)
    * tool count (when registered)
    * MCP server count (when connected; full list shown on hover via
      the Ctrl-K palette later if it gets large)
    * context-window percent
    * mode label
    """
    hud = state.hud
    title = hud.session_title or f"session {hud.session_id[:8]}"

    runs: list[tuple[str, str]] = [
        (f"{LAVENDER.hex} bold", title),
        (f"{OVERLAY0.hex}", "  │  "),
        (f"{TEAL.hex}", hud.backend),
        (f"{OVERLAY0.hex}", "/"),
        (f"{SAPPHIRE.hex}", hud.model),
    ]

    if hud.branch:
        runs.append((f"{OVERLAY0.hex}", "  │  "))
        runs.append((f"{GREEN.hex}", f"⎇ {hud.branch}"))

    if hud.tool_count > 0:
        runs.append((f"{OVERLAY0.hex}", "  │  "))
        runs.append((f"{YELLOW.hex}", "🔧 "))
        runs.append((f"{TEXT.hex}", f"{hud.tool_count} tools"))

    if hud.mcp_servers:
        runs.append((f"{OVERLAY0.hex}", "  │  "))
        # "OK" = connected OR unknown (clean connection, just no tools
        # exposed, or routed externally as on Codex). Only ``failed``
        # — i.e. servers in MCPBackend.connection_errors — count
        # against the badge.
        ok = sum(
            1 for s in hud.mcp_servers if s.get("state") in ("connected", "unknown")
        )
        failed = sum(1 for s in hud.mcp_servers if s.get("state") == "failed")
        total = len(hud.mcp_servers)
        label_color = (
            f"{GREEN.hex}"
            if failed == 0 and ok > 0
            else f"{YELLOW.hex}"
            if failed > 0 and ok > 0
            else f"{RED.hex} bold"
            if failed > 0
            else f"{PEACH.hex}"
        )
        runs.append((label_color, f"MCP {ok}/{total}"))
        if failed > 0:
            runs.append((f"{RED.hex} bold", " ⚠"))
        if total <= 3:
            runs.append((f"{OVERLAY0.hex}", "  "))
            _state_colors: dict[str, str] = {
                "connected": f"{GREEN.hex}",
                "failed": f"{RED.hex} bold",
                "unknown": f"{OVERLAY0.hex}",
            }
            for i, srv in enumerate(hud.mcp_servers):
                if i:
                    runs.append((f"{OVERLAY0.hex}", " "))
                # ``srv_state`` rather than ``state`` to avoid
                # shadowing the outer ``state: TUIState`` parameter.
                srv_state = str(srv.get("state", "unknown"))
                dot_color = _state_colors.get(srv_state, f"{OVERLAY0.hex}")
                runs.append((dot_color, "● "))
                name_color = (
                    f"{TEXT.hex}" if srv_state == "connected" else f"{SUBTEXT0.hex}"
                )
                runs.append((name_color, str(srv.get("name", "?"))))

    runs.append((f"{OVERLAY0.hex}", "  │  "))
    ctx_color = (
        f"{RED.hex} bold"
        if hud.ctx_pct >= 90
        else f"{PEACH.hex}"
        if hud.ctx_pct >= 70
        else f"{SUBTEXT0.hex}"
    )
    runs.append((ctx_color, f"ctx {hud.ctx_pct}%"))

    runs.append((f"{OVERLAY0.hex}", "  │  "))
    runs.append((f"{MAUVE.hex}", hud.mode.value))

    return FormattedText(runs)


def toolbar_text(state: TUIState) -> FormattedText:
    """Render the bottom toolbar: hotkeys, counters, agent tree.

    The agent tree is appended on subsequent lines when
    :attr:`TUIState.show_agent_panel` is true and there are running
    supervised agents.
    """
    hud = state.hud

    runs: list[tuple[str, str]] = [
        (f"{BLUE.hex} bold", "^C"),
        (f"{SUBTEXT0.hex}", " quit  "),
        (f"{BLUE.hex} bold", "^K"),
        (f"{SUBTEXT0.hex}", " palette  "),
        (f"{BLUE.hex} bold", "F2"),
        (f"{SUBTEXT0.hex}", " agents  "),
        (f"{BLUE.hex} bold", "Esc+⏎"),
        (f"{SUBTEXT0.hex}", " newline"),
    ]

    runs.append((f"{OVERLAY0.hex}", "   │   "))

    # Agent counter — bright green dot when at least one agent is
    # actively running, dim grey otherwise. Breakdown by status when
    # multiple are present so the user can tell "2 agents 1 running"
    # apart from "2 agents both running" without opening the panel.
    total_agents = len(hud.running_agents)
    running = sum(1 for a in hud.running_agents if a.status == "running")
    if total_agents > 0:
        glyph_color = f"{GREEN.hex}" if running else f"{PEACH.hex}"
        runs.append((glyph_color, "● "))
        if total_agents == running:
            runs.append((f"{TEXT.hex}", f"{total_agents} agent"))
            if total_agents != 1:
                runs.append((f"{TEXT.hex}", "s"))
        else:
            runs.append(
                (f"{TEXT.hex}", f"{total_agents} agents · {running} running"),
            )
    else:
        runs.append((f"{OVERLAY0.hex}", "○ 0 agents"))
    runs.append((f"{OVERLAY0.hex}", "   "))
    runs.append((f"{TEXT.hex}", f"tasks {hud.task_count}"))

    return FormattedText(runs)


def agent_panel_text(state: TUIState) -> FormattedText:
    """Render the right-side subagent panel (toggled with ``Ctrl+G``).

    One row per running agent showing a coloured status glyph, the
    agent's name, elapsed time since spawn, iteration count, and the
    most recent tool the agent invoked. Returns an empty
    :class:`FormattedText` when the panel is collapsed
    (``state.show_agent_panel`` is ``False``) — the layout's
    :class:`ConditionalContainer` then drops the side column entirely
    so the transcript reclaims the horizontal real estate.
    """
    if not state.show_agent_panel:
        return FormattedText([])

    hud = state.hud
    runs: list[tuple[str, str]] = []

    # Header row — distinguishes the panel from the transcript next to
    # it and makes the empty state self-explanatory.
    runs.append((f"{LAVENDER.hex} bold", "Agents"))
    runs.append((f"{OVERLAY0.hex}", "  "))
    runs.append((f"{SUBTEXT0.hex}", f"({len(hud.running_agents)})"))
    runs.append(("", "\n"))
    runs.append((f"{OVERLAY0.hex}", "─" * 24))
    runs.append(("", "\n"))

    if not hud.running_agents:
        runs.append((f"{OVERLAY0.hex}", "no agents running"))
        return FormattedText(runs)

    status_colors: dict[str, str] = {
        "running": f"{GREEN.hex}",
        "waiting": f"{PEACH.hex}",
        "pending": f"{OVERLAY1.hex}",
    }
    for agent in hud.running_agents:
        glyph_color = status_colors.get(agent.status, f"{SUBTEXT0.hex}")
        runs.append((glyph_color, "● "))
        runs.append((f"{TEXT.hex} bold", agent.name))
        runs.append(("", "\n"))
        # Indent the metadata one column under the glyph.
        runs.append((f"{OVERLAY0.hex}", "  "))
        runs.append((f"{SUBTEXT0.hex}", agent.status))
        runs.append((f"{OVERLAY0.hex}", " · "))
        runs.append((f"{SUBTEXT0.hex}", agent.elapsed_display))
        if agent.iteration_count:
            runs.append((f"{OVERLAY0.hex}", " · "))
            runs.append(
                (f"{SUBTEXT0.hex}", f"#{agent.iteration_count}"),
            )
        runs.append(("", "\n"))
        if agent.last_tool:
            runs.append((f"{OVERLAY0.hex}", "  ↳ "))
            runs.append((f"{YELLOW.hex}", _truncate(agent.last_tool, 24)))
            runs.append(("", "\n"))
    # Drop the trailing newline so the surrounding window doesn't
    # show an extra blank row.
    if runs and runs[-1] == ("", "\n"):
        runs.pop()
    return FormattedText(runs)
