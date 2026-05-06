"""obscura.cli.promptkit.status — prompt status state and banner/toolbar.

Owns the live state objects rendered by the legacy bordered REPL and
(soon) the full-screen Textual TUI:

  * ``StreamingStatus`` — mutable spinner/preview state pumped by the
    stream renderer and read by the prompt's bottom toolbar callable.
  * ``PromptStatus`` + ``RunningAgentInfo`` — aggregate session state
    rendered above the input box on each prompt cycle.

Also exposes:

  * ``animate_spinner`` — background asyncio task that ticks the
    spinner frame and invalidates the prompt_toolkit app.
  * ``print_status_banner`` — Rich-rendered status line above the input.
  * ``_build_toolbar_html`` — bottom-toolbar HTML for prompt_toolkit.
  * ``_get_git_branch`` — best-effort current branch (used by callers
    populating ``PromptStatus.branch``).

Consumers
---------
* ``obscura.cli.promptkit.session_factory.create_prompt_session`` —
  reads ``StreamingStatus`` / ``PromptStatus`` to render the toolbar.
* ``obscura.cli._repl_loop`` — populates the status objects each cycle.
* ``obscura.cli.prompt`` (legacy back-compat shim).
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import subprocess
from dataclasses import dataclass, field

from prompt_toolkit.application import get_app_or_none
from rich.markup import escape as markup_escape

from obscura.cli.render import console
from obscura.cli.renderer.modern.theme import (
    BLUE as _C_BLUE,
    GREEN as _C_GREEN,
    OVERLAY0 as _C_OVERLAY0,
    PEACH as _C_PEACH,
    RED as _C_RED,
    TEAL as _C_TEAL,
    TEXT as _C_TEXT,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# StreamingStatus — shared mutable state for toolbar spinner
# ---------------------------------------------------------------------------

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@dataclass
class StreamingStatus:
    """Mutable bag updated by StreamRenderer, read by the toolbar callable."""

    active: bool = False
    text: str = ""  # e.g. "thinking...", "running edit_file..."
    preview: str = ""  # thinking-delta preview snippet
    spinner_idx: int = 0

    @property
    def spinner_char(self) -> str:
        return _SPINNER_FRAMES[self.spinner_idx % len(_SPINNER_FRAMES)]

    def reset(self) -> None:
        self.active = False
        self.text = ""
        self.preview = ""


async def animate_spinner(status: StreamingStatus) -> None:
    """Background task: advance spinner frame + invalidate prompt toolbar."""
    while True:
        await asyncio.sleep(0.1)
        if not status.active:
            continue
        status.spinner_idx = (status.spinner_idx + 1) % len(_SPINNER_FRAMES)
        try:
            app = get_app_or_none()
            if app is not None:
                app.invalidate()
        except Exception:
            logger.debug("suppressed exception in animate_spinner", exc_info=True)


# ---------------------------------------------------------------------------
# PromptStatus — live state shown in the banner above the input box
# ---------------------------------------------------------------------------


@dataclass
class RunningAgentInfo:
    """Snapshot of a single running agent for display."""

    name: str
    status: str = "running"  # running | waiting | pending
    elapsed_s: float = 0.0
    iteration_count: int = 0
    last_tool: str = ""

    @property
    def elapsed_display(self) -> str:
        s = int(self.elapsed_s)
        if s < 60:
            return f"{s}s"
        m, sec = divmod(s, 60)
        return f"{m}m{sec:02d}s"


@dataclass
class PromptStatus:
    """Mutable state bag read by print_status_banner() on every prompt call."""

    model: str = ""
    branch: str = ""
    ctx_pct: int = 0  # 0-100
    ctx_tokens: int = 0
    ctx_window: int = 0
    mode: str = ""
    session_id: str = ""
    session_title: str = ""  # auto-generated or manual title
    running_agents: list[str] = field(default_factory=list[str])
    agent_details: list[RunningAgentInfo] = field(
        default_factory=list[RunningAgentInfo]
    )
    task_count: int = 0


def _get_git_branch() -> str:
    """Return the current git branch name, or '' if not in a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=1,
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
            return branch if branch and branch != "HEAD" else ""
    except Exception:
        logger.debug("suppressed exception in _get_git_branch", exc_info=True)
    return ""


def print_status_banner(status: PromptStatus) -> None:
    """Print a Claude Code-style status line above the input box.

    Format:
      Session Title (abc12345)
      claude-opus-4 · 12.3k tokens (42%) · ⎇ main · code

    Uses Rich markup via the shared console.
    """
    # Line 1: Session title (if available)
    if status.session_id:
        short_id = status.session_id[:8]
        if status.session_title:
            console.print(
                f"  [bold {_C_TEXT.hex}]{markup_escape(status.session_title)}[/]"
                f"  [dim]({short_id})[/]",
                highlight=False,
            )

    # Line 2: model · tokens · branch · mode
    parts: list[str] = []

    if status.model:
        parts.append(f"[bold {_C_BLUE.hex}]{markup_escape(status.model)}[/]")

    if status.ctx_pct > 0 or status.ctx_tokens > 0:
        pct = status.ctx_pct
        if pct >= 80:
            color = f"bold {_C_RED.hex}"
        elif pct >= 60:
            color = _C_PEACH.hex
        else:
            color = _C_GREEN.hex
        if status.ctx_tokens:
            t = status.ctx_tokens
            if t >= 1000:
                ctx_str = f"{t / 1000:.1f}k tokens ({pct}%)"
            else:
                ctx_str = f"{t} tokens ({pct}%)"
        else:
            ctx_str = f"ctx {pct}%"
        parts.append(f"[{color}]{ctx_str}[/]")

    if status.branch:
        parts.append(f"[{_C_TEAL.hex}]⎇ {markup_escape(status.branch)}[/]")

    if status.mode:
        parts.append(f"[dim]{markup_escape(status.mode)} mode[/]")

    if parts:
        sep = "  [dim]·[/]  "
        console.print(f"  {sep.join(parts)}", highlight=False)

    # Running agents line
    if status.running_agents:
        agent_labels = [
            f"[bold {_C_GREEN.hex}]{markup_escape(name)}[/] [{_C_GREEN.hex}]●[/]"
            for name in status.running_agents
        ]
        console.print(
            f"  [dim]agents:[/] {' '.join(agent_labels)}",
            highlight=False,
        )


def _build_toolbar_html(prompt_status: PromptStatus | None) -> str:
    """Build the bottom toolbar: status line + optional agent panel.

    Line 1: session · model · context % · hints
    Line 2+: running agent tree (only when agents are active)
    """
    if prompt_status is None:
        return "  <b>!</b> for bash · <b>/help</b> for commands · <b>esc+enter</b> for newline"

    left_parts: list[str] = []
    right_parts: list[str] = []

    # Session title or short ID
    if prompt_status.session_title:
        left_parts.append(f"<b>{_html.escape(prompt_status.session_title)}</b>")
    elif prompt_status.session_id:
        left_parts.append(
            f"<style fg='{_C_OVERLAY0.hex}'>"
            f"{_html.escape(prompt_status.session_id[:8])}</style>"
        )

    # Model
    if prompt_status.model:
        left_parts.append(_html.escape(prompt_status.model))

    # Context usage
    if prompt_status.ctx_pct > 0:
        pct = prompt_status.ctx_pct
        if pct >= 80:
            ctx_str = f"<style fg='{_C_RED.hex}'>{pct}% context</style>"
        elif pct >= 60:
            ctx_str = f"<style fg='{_C_PEACH.hex}'>{pct}% context</style>"
        else:
            ctx_str = f"{pct}% context"
        left_parts.append(ctx_str)

    # Task count if active
    if prompt_status.task_count > 0:
        left_parts.append(f"<b>{prompt_status.task_count}</b> tasks")

    # Agent count
    if prompt_status.running_agents:
        n = len(prompt_status.running_agents)
        left_parts.append(f"<b>{n}</b> agent{'s' if n != 1 else ''}")

    # Shortcut hints (right side)
    right_parts.append("<b>!</b> bash")
    right_parts.append("<b>/help</b>")
    right_parts.append("<b>esc+enter</b> newline")

    left = " · ".join(left_parts)
    right = " · ".join(right_parts)
    status_line = f"  {left}    {right}"

    # ── Agent panel (tree-connected lines below status) ─────────────────
    agents = prompt_status.agent_details
    if not agents:
        return status_line

    lines = [status_line]
    for i, ag in enumerate(agents):
        is_last = i == len(agents) - 1
        tree = "└─" if is_last else "├─"

        # Status indicator
        if ag.status == "running":
            bullet = "<style fg='#a6e3a1'>●</style>"
        elif ag.status == "waiting":
            bullet = "<style fg='#fab387'>○</style>"
        else:
            bullet = "<style fg='#6c7086'>◌</style>"

        name_esc = _html.escape(ag.name)
        elapsed = _html.escape(ag.elapsed_display)

        agent_line = (
            f"  <style fg='#45475a'>{tree}</style> "
            f"{bullet} <b>{name_esc}</b>"
            f"  <style fg='#6c7086'>{elapsed}</style>"
        )

        if ag.iteration_count > 0:
            agent_line += f"  <style fg='#6c7086'>{ag.iteration_count} turns</style>"

        lines.append(agent_line)

        # Last tool activity line
        if ag.last_tool and ag.status == "running":
            pad = "   " if is_last else "<style fg='#45475a'>│</style>  "
            tool_esc = _html.escape(ag.last_tool)
            lines.append(f"  {pad}<style fg='#89b4fa'>⍿ {tool_esc}</style>")

    return "\n".join(lines)
