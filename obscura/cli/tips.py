"""
obscura.cli.tips — Contextual tips and feature suggestions.

Shows helpful tips based on user behavior to aid feature discovery.
Tips have conditions, cooldowns, and priority ordering.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Tip:
    """A contextual tip for the user."""

    id: str
    message: str
    condition: str  # "after_edit" | "after_search" | "long_session" | "first_use" | "always"
    cooldown_hours: float = 24.0
    priority: int = 0  # Higher = shown first


# Built-in tips.
TIPS: list[Tip] = [
    Tip("commit", "Use `/commit` to create AI-generated commit messages from your changes.", "after_edit", 48),
    Tip("review", "Use `/review` for AI-powered code review of pending changes.", "after_edit", 48),
    Tip("compact", "Context getting large? Use `/compact` to compress conversation history.", "long_session", 4),
    Tip("effort", "Adjust response depth with `/effort low|medium|high|max`.", "first_use", 168),
    Tip("fast", "Toggle terse mode with `/fast` for quick answers.", "first_use", 168),
    Tip("resume", "Resume previous sessions with `/resume [search]`.", "first_use", 168),
    Tip("agent", "Spawn specialized agents with `/agent spawn <name>`.", "first_use", 168),
    Tip("vim", "Toggle vim keybindings with `/vim`.", "first_use", 336),
    Tip("cost", "Check token usage and costs with `/cost`.", "long_session", 12),
    Tip("doctor", "Run `/doctor` to check your environment setup.", "first_use", 336),
    Tip("permissions", "Switch permission modes with `/permissions plan|accept_edits`.", "first_use", 168),
    Tip("export", "Export conversation with `/export md|txt|json`.", "long_session", 24),
    Tip("security", "Run `/security-review` before merging security-sensitive changes.", "after_edit", 72),
    Tip("worktree", "Use worktree tools for isolated git work.", "after_search", 72),
    Tip("voice", "Enable voice input with `/voice on` (requires SoX).", "first_use", 336),
    Tip("init", "Run `/init` to generate an OBSCURA.md for this repository.", "first_use", 336),
    Tip("kairos", "Set `OBSCURA_KAIROS=1` to enable autonomous background monitoring.", "first_use", 336),
    Tip("coordinator", "Use `/coordinator on` for multi-worker agent orchestration.", "first_use", 336),
    Tip("search_tools", "Use `/search-tools <query>` to find available tools.", "after_search", 48),
    Tip("skills", "Use `/skill list` to see available skills.", "first_use", 168),
]


class TipScheduler:
    """Manages tip display with cooldowns and conditions."""

    def __init__(self) -> None:
        self._shown: dict[str, float] = {}  # tip_id → last_shown_timestamp
        self._message_count = 0
        self._edit_count = 0
        self._search_count = 0

    def record_message(self) -> None:
        self._message_count += 1

    def record_edit(self) -> None:
        self._edit_count += 1

    def record_search(self) -> None:
        self._search_count += 1

    def get_tip(self) -> str | None:
        """Get the next applicable tip, or None if nothing to show."""
        now = time.time()
        condition = self._current_condition()

        candidates = [
            t for t in TIPS
            if t.condition == condition or t.condition == "always"
        ]
        # Filter by cooldown.
        candidates = [
            t for t in candidates
            if t.id not in self._shown
            or (now - self._shown[t.id]) > t.cooldown_hours * 3600
        ]
        if not candidates:
            return None

        # Pick highest priority.
        candidates.sort(key=lambda t: t.priority, reverse=True)
        tip = candidates[0]
        self._shown[tip.id] = now
        return f"Tip: {tip.message}"

    def _current_condition(self) -> str:
        if self._message_count <= 1:
            return "first_use"
        if self._edit_count > 0:
            cond = "after_edit"
            self._edit_count = 0
            return cond
        if self._search_count > 0:
            cond = "after_search"
            self._search_count = 0
            return cond
        if self._message_count > 20:
            return "long_session"
        return "always"

    def reset(self) -> None:
        self._shown.clear()
        self._message_count = 0
        self._edit_count = 0
        self._search_count = 0
