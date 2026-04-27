"""obscura.kairos.away_summary — Summarize what happened while the user was away.

Generates a brief 1-3 sentence summary of the current work context
when the user returns after an idle period.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# Minimum idle time before generating away summary (seconds).
AWAY_THRESHOLD_S = 300.0  # 5 minutes

AWAY_SUMMARY_PROMPT = """\
The user stepped away and is coming back. Write exactly 1-3 short sentences.

Start by stating the high-level task — what they are building or debugging.
Next: the concrete next step. Skip status reports and commit recaps.

Be specific about file names and function names where relevant.
"""


class AwaySummaryTracker:
    """Tracks user activity and generates away summaries.

    Usage::

        tracker = AwaySummaryTracker()
        tracker.mark_active()  # Call on each user interaction
        # ... user goes idle ...
        if tracker.should_generate():
            summary = await tracker.generate(message_history)
    """

    def __init__(self, threshold_s: float = AWAY_THRESHOLD_S) -> None:
        self._threshold_s = threshold_s
        self._last_active = time.time()
        self._last_summary_at = 0.0

    def mark_active(self) -> None:
        """Mark user as currently active."""
        self._last_active = time.time()

    @property
    def idle_seconds(self) -> float:
        """Seconds since last user activity."""
        return time.time() - self._last_active

    def should_generate(self) -> bool:
        """Check if an away summary should be generated."""
        if self.idle_seconds < self._threshold_s:
            return False
        # Don't generate if we already did since last activity.
        return not self._last_summary_at > self._last_active


async def generate_away_summary(
    message_history: list[tuple[str, str]],
    *,
    max_recent: int = 30,
) -> str:
    """Generate a brief away summary from recent message history.

    Returns a 1-3 sentence summary of what was being worked on,
    or an empty string if there's not enough context.
    """
    if len(message_history) < 2:
        return ""

    # Build context from recent messages.
    recent = message_history[-max_recent:]
    context_parts: list[str] = []
    for role, text in recent:
        preview = text[:300].replace("\n", " ")
        context_parts.append(f"[{role}] {preview}")

    context = "\n".join(context_parts)

    # Try LLM-based summary first.
    try:
        llm_summary = await _generate_llm_summary(context)
        if llm_summary:
            return f"Welcome back. {llm_summary}"
    except Exception:
        pass

    # Fallback: extract first sentence of last assistant message.
    for role, text in reversed(recent):
        if role == "assistant":
            sentences = text.split(". ")
            if sentences:
                summary = sentences[0].strip()
                if len(summary) > 200:
                    summary = summary[:197] + "..."
                return f"Welcome back. {summary}."
    return ""


async def _generate_llm_summary(context: str) -> str:
    """Call the LLM with AWAY_SUMMARY_PROMPT + context to generate a summary.

    Returns an empty string if the LLM is unavailable or fails.
    """
    try:
        from obscura.core.client import ObscuraClient
        from obscura.core.config import ObscuraConfig

        cfg = ObscuraConfig.load()
        prompt = f"{AWAY_SUMMARY_PROMPT}\n\nRecent conversation:\n{context[:3000]}"
        async with ObscuraClient(
            cfg.default_backend,
            model=cfg.default_model or None,
            system_prompt="You are a concise assistant summarizing recent work context.",
        ) as client:
            result = await client.run_loop_to_completion(prompt, max_turns=1)
            # Strip to first 3 sentences max.
            sentences = result.strip().split(". ")
            summary = ". ".join(sentences[:3]).strip()
            if summary and not summary.endswith("."):
                summary += "."
            return summary
    except Exception:
        logger.debug("LLM away summary failed, using fallback", exc_info=True)
        return ""
