"""obscura.core.context_window — Token counting and context window management.

Port of openclaw's context-window-guard.ts, enhanced with Claude Code-style
multi-strategy compaction thresholds.

Provides:
- get_context_window(model_id): model context window size lookup
- estimate_tokens(text): tiktoken-based token estimation with fallback
- evaluate_context_status(messages, model_id): warn/block evaluation
- get_compact_thresholds(model_id): model-aware compaction trigger levels
- CompactThresholds: snip / compact / critical token thresholds

Constants mirror openclaw exactly:
  CONTEXT_WINDOW_HARD_MIN_TOKENS = 16_000   (block agent start)
  CONTEXT_WINDOW_WARN_BELOW_TOKENS = 32_000  (warn user)

Compaction thresholds (Claude Code-style):
  snip_at      — snip verbose tool outputs (lightest)
  compact_at   — full history compaction with LLM summarization
  critical_at  — aggressive compaction, drop + summarize immediately
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, cast
from typing_extensions import override

logger = logging.getLogger(__name__)

_tokenizer: Any | None = None
_tokenizer_ready = False

# Mirrors openclaw's context-window-guard.ts constants
CONTEXT_WINDOW_HARD_MIN_TOKENS = 16_000  # Block if available tokens < this
CONTEXT_WINDOW_WARN_BELOW_TOKENS = 32_000  # Warn if available tokens < this

# Snip compact: truncate individual tool outputs above this token count
SNIP_TOOL_OUTPUT_THRESHOLD = 10_000  # tokens

# Model context windows
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # Anthropic
    "claude-opus-4-5": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-3-5": 200_000,
    "claude-opus-4": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-3-opus-20240229": 200_000,
    "claude-3-5-opus": 200_000,
    "claude-3-sonnet-20240229": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-haiku-20240307": 200_000,
    "claude-3-5-haiku": 200_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    # Google
    "gemini-2.0-flash": 1_000_000,
    "gemini-2.0-flash-lite": 1_000_000,
    "gemini-1.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_000_000,
    # Fallback
    "default": 100_000,
}


# ---------------------------------------------------------------------------
# CompactThresholds — model-aware multi-strategy compaction triggers
# ---------------------------------------------------------------------------


@dataclass
class CompactThresholds:
    """Token thresholds that trigger progressively aggressive compaction.

    Mirrors Claude Code's tiered approach:
    - snip_at:     Snip verbose tool outputs individually (lightest)
    - compact_at:  Full history compaction with LLM summarization
    - critical_at: Aggressive drop + summarize, reactive mid-turn
    - preserve_recent: Number of recent message pairs to always keep
    """

    snip_at: int
    """Token usage level that triggers snip compact on tool outputs."""

    compact_at: int
    """Token usage level that triggers full history compaction."""

    critical_at: int
    """Token usage level that triggers aggressive reactive compaction."""

    preserve_recent: int
    """Number of recent message pairs (assistant+user) to always preserve."""

    context_window: int
    """Total context window for reference."""

    def usage_tier(self, used_tokens: int) -> str:
        """Return the compaction tier for a given token usage level.

        Returns one of: "ok", "snip", "compact", or "critical".
        """
        if used_tokens >= self.critical_at:
            return "critical"
        if used_tokens >= self.compact_at:
            return "compact"
        if used_tokens >= self.snip_at:
            return "snip"
        return "ok"


# Default threshold ratios (fraction of context window)
_THRESHOLD_PROFILES: dict[str, tuple[float, float, float, int]] = {
    # (snip_ratio, compact_ratio, critical_ratio, preserve_pairs)
    #
    # Large-context models (200K+): more aggressive — lots of room to work
    "large": (0.60, 0.75, 0.90, 6),
    # Medium-context models (100K-200K)
    "medium": (0.55, 0.70, 0.85, 4),
    # Small-context models (<100K): conservative — less room
    "small": (0.50, 0.65, 0.80, 3),
}


def get_compact_thresholds(model_id: str) -> CompactThresholds:
    """Return model-aware compaction thresholds.

    Selects a threshold profile based on the model's context window size,
    then scales to absolute token counts.
    """
    cw = get_context_window(model_id)

    if cw >= 200_000:
        profile = "large"
    elif cw >= 100_000:
        profile = "medium"
    else:
        profile = "small"

    snip_r, compact_r, critical_r, preserve = _THRESHOLD_PROFILES[profile]

    return CompactThresholds(
        snip_at=int(cw * snip_r),
        compact_at=int(cw * compact_r),
        critical_at=int(cw * critical_r),
        preserve_recent=preserve,
        context_window=cw,
    )


def get_context_window(model_id: str) -> int:
    """Resolve context window size for a model.

    Priority: exact match → prefix match → default (100K).
    """
    if model_id in MODEL_CONTEXT_WINDOWS:
        return MODEL_CONTEXT_WINDOWS[model_id]
    m = model_id.lower()
    for k, v in MODEL_CONTEXT_WINDOWS.items():
        if k == "default":
            continue
        kl = k.lower()
        if m.startswith(kl) or kl.startswith(m):
            return v
    logger.debug(f"Unknown model '{model_id}', using default context window (100K)")
    return MODEL_CONTEXT_WINDOWS["default"]


def estimate_tokens(text: str) -> int:
    """Estimate token count using tiktoken if available, else word heuristic.

    tiktoken (cl100k_base) is accurate to ~1%.
    Word heuristic (~0.75 words/token) is accurate to ~15% for prose, worse for code.
    """
    if not text:
        return 0
    return _estimate_tokens_cached(text)


def _get_tokenizer() -> Any | None:
    """Load and cache tokenizer once per process."""
    global _tokenizer, _tokenizer_ready
    if _tokenizer_ready:
        return _tokenizer
    _tokenizer_ready = True
    try:
        import tiktoken

        tk: Any = tiktoken
        try:
            _tokenizer = tk.get_encoding("cl100k_base")
        except Exception:
            _tokenizer = tk.get_encoding("gpt2")
    except ImportError:
        _tokenizer = None
    return _tokenizer


@lru_cache(maxsize=8192)
def _estimate_tokens_cached(text: str) -> int:
    """Cached token estimator for repeated prompt/history fragments."""
    enc = _get_tokenizer()
    if enc is not None:
        return len(enc.encode(text))
    return max(1, int(len(text.split()) / 0.75))


def estimate_message_tokens(msg: Any) -> int:
    """Estimate tokens for a single message, including per-message overhead."""
    total = 0
    content: Any = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = cast(dict[str, Any], msg).get("content")
    if content is None:
        content = str(cast(object, msg))

    if isinstance(content, str):
        total += estimate_tokens(content)
    elif isinstance(content, list):
        for block in cast(list[Any], content):
            if isinstance(block, dict):
                block_dict = cast(dict[str, Any], block)
                text: Any = block_dict.get("text", "")
                if not text:
                    try:
                        text = json.dumps(block_dict)
                    except Exception:
                        text = str(block_dict)
                total += estimate_tokens(str(text))
            elif hasattr(block, "text"):
                total += estimate_tokens(str(block.text))
            else:
                total += estimate_tokens(str(block))
    else:
        total += estimate_tokens(str(content))

    total += 4  # per-message role/format overhead
    return total


def estimate_messages_tokens(messages: list[Any]) -> int:
    """Estimate total tokens for a list of Message objects.

    Handles both string and list content (tool_use blocks, text blocks).
    Adds 4 tokens overhead per message for role/formatting.
    """
    return sum(estimate_message_tokens(msg) for msg in messages)


@dataclass
class ContextStatus:
    """Result of a context window evaluation."""

    available_tokens: int
    """Tokens remaining after current usage (can be negative if over limit)."""

    used_tokens: int
    """Tokens used by system prompt + messages."""

    context_window: int
    """Total model context window in tokens."""

    should_warn: bool
    """True if available_tokens < CONTEXT_WINDOW_WARN_BELOW_TOKENS (32K)."""

    should_block: bool
    """True if available_tokens < CONTEXT_WINDOW_HARD_MIN_TOKENS (16K)."""

    usage_pct: float
    """Fraction of context window used (0.0–1.0+)."""

    compact_tier: str = "ok"
    """Current compaction tier: 'ok', 'snip', 'compact', or 'critical'."""

    @override
    def __str__(self) -> str:
        s = "BLOCK" if self.should_block else ("WARN" if self.should_warn else "OK")
        tier = f" tier={self.compact_tier}" if self.compact_tier != "ok" else ""
        return (
            f"[{s}{tier}] {self.used_tokens:,}/{self.context_window:,} tokens "
            f"({self.usage_pct:.1%} used, {self.available_tokens:,} available)"
        )


def evaluate_context_status(
    messages: list[Any],
    model_id: str,
    system_prompt: str = "",
    reserve_tokens: int = 4096,
) -> ContextStatus:
    """Evaluate context window usage and return status with warn/block flags.

    Mirrors openclaw's evaluateContextWindowGuard() + resolveContextWindowInfo().

    Args:
        messages: Current message list
        model_id: Model identifier for context window lookup
        system_prompt: System prompt text (counted toward used tokens)
        reserve_tokens: Reserve for model response (default: 4096)

    Returns:
        ContextStatus with should_warn and should_block flags set

    """
    cw = get_context_window(model_id)
    sys_tokens = estimate_tokens(system_prompt) if system_prompt else 0
    msg_tokens = estimate_messages_tokens(messages)
    used = sys_tokens + msg_tokens
    available = cw - used - reserve_tokens
    pct = used / cw if cw > 0 else 0.0

    thresholds = get_compact_thresholds(model_id)
    tier = thresholds.usage_tier(used)

    status = ContextStatus(
        available_tokens=available,
        used_tokens=used,
        context_window=cw,
        should_warn=available < CONTEXT_WINDOW_WARN_BELOW_TOKENS,
        should_block=available < CONTEXT_WINDOW_HARD_MIN_TOKENS,
        usage_pct=pct,
        compact_tier=tier,
    )

    if status.should_block:
        logger.warning("Context window CRITICAL: %s", status)
    elif status.should_warn:
        logger.warning("Context window WARNING: %s", status)
    else:
        logger.debug("Context window OK: %s", status)

    return status


# ---------------------------------------------------------------------------
# Token budget helpers — used by tools to cap response sizes.
# ---------------------------------------------------------------------------

MAX_FILE_READ_TOKENS = 100_000
"""Hard ceiling for file content returned by ``read_text_file``."""

MAX_WEB_FETCH_TOKENS = 50_000
"""Hard ceiling for web content returned by ``web_fetch``."""


def truncate_to_token_budget(
    text: str,
    max_tokens: int,
) -> tuple[str, bool]:
    """Truncate *text* so it fits within *max_tokens*.

    Returns ``(possibly_truncated_text, was_truncated)``.

    The function uses a binary-search approach to avoid calling
    ``estimate_tokens`` on the full string more than necessary.
    """
    token_count = estimate_tokens(text)
    if token_count <= max_tokens:
        return text, False

    # Heuristic: ~4 chars per token on average.
    approx_chars = max_tokens * 4
    truncated = text[:approx_chars]

    # Refine: shrink until within budget.
    while estimate_tokens(truncated) > max_tokens and len(truncated) > 100:
        truncated = truncated[: int(len(truncated) * 0.9)]

    return truncated, True
