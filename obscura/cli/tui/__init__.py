"""obscura.cli.tui — Full-screen prompt-toolkit TUI front-end.

Sibling to the legacy Click + bordered-prompt REPL (``obscura/cli/_repl_loop.py``).
Both surfaces share:

* the engine (``obscura.composition.repl.build_repl_session`` →
  :class:`obscura.composition.session.AgentSession`)
* the widget kit (``obscura.cli.promptkit``)
* the renderer protocol (``obscura.cli.renderer.protocol.RendererProtocol``)
* the channel taxonomy (``obscura.cli.renderer.channels``)

The TUI differs in one place only — instead of reading input via a
prompt-toolkit ``PromptSession`` and writing events to stdout, it owns a
full-screen ``prompt_toolkit.Application`` whose layout has dedicated
windows for transcript, live-region, notifications, banner, input box,
and toolbar. Modal overlays (tool-approval, command palette, agent
inspector) appear as floats over the main layout.

Public entry point: :func:`run_tui` — invoked by the
``obscura tui`` Click subcommand.
"""

from __future__ import annotations

from obscura.cli.tui.runtime import run_tui
from obscura.cli.tui.state import (
    HUDState,
    LiveRegionKind,
    LiveRegionState,
    NotificationItem,
    RunningAgentSnapshot,
    StyledRun,
    ToolApprovalRequest,
    TranscriptEntry,
    TranscriptKind,
    TUIMode,
    TUIState,
)

__all__ = [
    "HUDState",
    "LiveRegionKind",
    "LiveRegionState",
    "NotificationItem",
    "RunningAgentSnapshot",
    "StyledRun",
    "ToolApprovalRequest",
    "TUIMode",
    "TUIState",
    "TranscriptEntry",
    "TranscriptKind",
    "run_tui",
]
