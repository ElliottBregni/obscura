"""obscura.kairos.undercover — Stealth mode for public repositories.

When active, strips all AI attribution and internal references from
commits, PRs, and agent output to prevent revealing that an AI tool
was used. Auto-activates for public/open-source repositories.

Pattern from claude-code's ``undercover.ts``.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Internal repo patterns that should NOT trigger undercover mode.
_INTERNAL_REPO_PATTERNS = [
    "anthropic",
    "obscura-internal",
    "obscura-private",
]

UNDERCOVER_SYSTEM_PROMPT = """\
## UNDERCOVER MODE — CRITICAL

You are operating in UNDERCOVER mode in a PUBLIC/OPEN-SOURCE repository.

Your commit messages, PR titles, PR bodies, and all visible output
MUST NOT contain ANY of the following:

- The name "Obscura" or any AI tool names
- Model names or version numbers (e.g., "claude-4", "gpt-5")
- "Co-Authored-By" lines mentioning AI
- References to "AI agent", "LLM", "language model"
- Internal codenames or project references
- Any hint that you are an AI system

Write as a human developer would — describe what the code change does,
not how or who produced it. Use first person ("I fixed...", "I refactored...").

If asked about your identity, deflect naturally.
Do not blow your cover.
"""


def is_undercover() -> bool:
    """Check if undercover mode should be active.

    **ON by default.** Only disabled when explicitly set to off.
    """
    env_val = os.environ.get("OBSCURA_UNDERCOVER", "1").strip().lower()
    return env_val not in ("0", "false", "no", "off")


class UndercoverMode:
    """Manages undercover mode state and prompt injection."""

    def __init__(self) -> None:
        self._forced: bool | None = None  # None = auto-detect

    @property
    def is_active(self) -> bool:
        if self._forced is not None:
            return self._forced
        return is_undercover()

    def force(self, enabled: bool) -> None:
        """Force undercover mode on or off."""
        self._forced = enabled

    def auto(self) -> None:
        """Return to auto-detection mode."""
        self._forced = None

    def get_system_prompt_addition(self) -> str:
        """Return undercover instructions if mode is active."""
        if self.is_active:
            return UNDERCOVER_SYSTEM_PROMPT
        return ""

    def sanitize_commit_message(self, message: str) -> str:
        """Strip AI attribution from a commit message."""
        if not self.is_active:
            return message
        import re

        # Remove Co-Authored-By lines mentioning AI/Claude/Obscura.
        sanitized = re.sub(
            r"\n\s*Co-Authored-By:.*(?:Claude|Obscura|AI|Agent|noreply@).*",
            "",
            message,
            flags=re.IGNORECASE,
        )
        return sanitized.rstrip() + "\n"
