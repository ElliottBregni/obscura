"""
obscura.core.context_window — Token counting and context window management.

Port of openclaw's context-window-guard.ts.

Provides:
- get_context_window(model_id): model context window size lookup
- estimate_tokens(text): tiktoken-based token estimation with fallback
- evaluate_context_status(messages, model_id): warn/block evaluation

Constants mirror openclaw exactly:
  CONTEXT_WINDOW_HARD_MIN_TOKENS = 16_000   (block agent start)
  CONTEXT_WINDOW_WARN_BELOW_TOKENS = 32_000  (warn user)
"""

from __future__ import annotations

import logging
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

_TOKENIZER: Any | None = None
_TOKENIZER_READY = False

# Mirrors openclaw's context-window-guard.ts constants
CONTEXT_WINDOW_HARD_MIN_TOKENS = 16_000    # Block if available tokens < this
CONTEXT_WINDOW_WARN_BELOW_TOKENS = 32_000  # Warn if available tokens < this

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
    global _TOKENIZER, _TOKENIZER_READY
    if _TOKENIZER_READY:
        return _TOKENIZER
    _TOKENIZER_READY = True
    try:
        import tiktoken

        try:
            _TOKENIZER = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _TOKENIZER = tiktoken.get_encoding("gpt2")
    except ImportError:
        _TOKENIZER = None
    return _TOKENIZER


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
    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    if content is None:
        content = str(msg)

    if isinstance(content, str):
        total += estimate_tokens(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                text = block.get("text", "")
                if not text:
                    try:
                        text = json.dumps(block)
                    except Exception:
                        text = str(block)
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

    def __str__(self) -> str:
        s = "BLOCK" if self.should_block else ("WARN" if self.should_warn else "OK")
        return (
            f"[{s}] {self.used_tokens:,}/{self.context_window:,} tokens "
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

    status = ContextStatus(
        available_tokens=available,
        used_tokens=used,
        context_window=cw,
        should_warn=available < CONTEXT_WINDOW_WARN_BELOW_TOKENS,
        should_block=available < CONTEXT_WINDOW_HARD_MIN_TOKENS,
        usage_pct=pct,
    )

    if status.should_block:
        logger.warning("Context window CRITICAL: %s", status)
    elif status.should_warn:
        logger.warning("Context window WARNING: %s", status)
    else:
        logger.debug("Context window OK: %s", status)

    return status
