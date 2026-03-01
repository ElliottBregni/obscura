"""
obscura.core.compaction — Conversation history compaction and summarization.

Port of openclaw's compaction.ts + pruneHistoryForContextShare().

Provides:
- repair_tool_pairs(messages): Remove orphaned tool_result blocks after pruning
- compact_history(messages, model_id, backend): Full compaction pipeline
- summarize_messages(messages, model_id, backend): LLM-based summarization

Critical invariant (mirrors openclaw exactly):
  After EVERY message drop, call repair_tool_pairs() immediately.
  The Anthropic API raises "unexpected tool_use_id" if a tool_result
  references a tool_use block that no longer exists in history.

Constants mirror openclaw's compaction.ts:
  BASE_CHUNK_RATIO    = 0.4   (40% of budget per summary chunk)
  MIN_CHUNK_RATIO     = 0.15  (floor when messages are large)
  MAX_HISTORY_SHARE   = 0.5   (max fraction of context for history)
  FALLBACK_KEEP_LAST  = 20    (messages to keep if LLM summary fails)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from obscura.core.context_window import (
    CONTEXT_WINDOW_HARD_MIN_TOKENS,
    estimate_messages_tokens,
    estimate_tokens,
    evaluate_context_status,
    get_context_window,
)

logger = logging.getLogger(__name__)

# Mirrors openclaw's compaction.ts constants
BASE_CHUNK_RATIO = 0.4      # 40% of context budget per chunk
MIN_CHUNK_RATIO = 0.15      # Floor ratio for very large messages
MAX_HISTORY_SHARE = 0.5     # Max 50% of context window for history
FALLBACK_KEEP_LAST = 20     # Messages to keep if summarization fails
LARGE_MSG_THRESHOLD = 0.10  # Reduce chunk ratio if avg msg > 10% of context


def repair_tool_pairs(messages: list[Any]) -> list[Any]:
    """Remove orphaned tool_result blocks after history pruning.

    When messages are dropped during compaction, tool_result blocks may
    reference tool_use IDs that no longer exist in history. The Anthropic
    API raises 'unexpected tool_use_id' in this case.

    This function removes:
    1. tool_result blocks whose tool_use_id has no matching tool_use block
    2. Messages that become empty after removing orphaned blocks

    Args:
        messages: List of message objects (with .content or dict access)

    Returns:
        Cleaned message list with all tool pairs intact

    Note: Mirrors openclaw's repairToolUseResultPairing() exactly.
    """
    tool_use_ids: set[str] = set()

    for msg in messages:
        content = _get_content(msg)
        if isinstance(content, list):
            for block in content:
                if _get_block_type(block) == "tool_use":
                    tid = _get_block_id(block)
                    if tid:
                        tool_use_ids.add(tid)

    cleaned: list[Any] = []
    for msg in messages:
        content = _get_content(msg)
        role = _get_role(msg)

        if isinstance(content, list):
            new_blocks = []
            for block in content:
                if _get_block_type(block) == "tool_result":
                    tid = _get_block_tool_use_id(block)
                    if tid and tid not in tool_use_ids:
                        logger.debug(
                            "repair_tool_pairs: removing orphaned tool_result %s", tid
                        )
                        continue
                new_blocks.append(block)

            if not new_blocks:
                logger.debug(
                    "repair_tool_pairs: dropping empty %s message after repair", role
                )
                continue

            cleaned.append(_rebuild_message(msg, new_blocks))
        else:
            cleaned.append(msg)

    return cleaned


def _compute_adaptive_chunk_ratio(
    messages: list[Any],
    context_window: int,
    base_ratio: float = BASE_CHUNK_RATIO,
    min_ratio: float = MIN_CHUNK_RATIO,
) -> float:
    """Compute adaptive chunk ratio based on average message size.

    Mirrors openclaw's computeChunkRatio() logic.
    """
    if not messages or context_window <= 0:
        return base_ratio

    total_tokens = estimate_messages_tokens(messages)
    avg_tokens = total_tokens / len(messages)
    avg_fraction = avg_tokens / context_window

    if avg_fraction > LARGE_MSG_THRESHOLD:
        scale = LARGE_MSG_THRESHOLD / avg_fraction
        ratio = max(min_ratio, base_ratio * scale)
        logger.debug(
            "Adaptive chunk ratio: %.2f (avg msg %.1f%% of context)",
            ratio,
            avg_fraction * 100,
        )
        return ratio

    return base_ratio


async def summarize_messages(
    messages: list[Any],
    model_id: str,
    backend: Any,
    max_tokens: int = 2048,
) -> str:
    """Summarize a list of messages using the LLM backend."""
    if not messages:
        return ""

    lines: list[str] = []
    for msg in messages:
        role = _get_role(msg) or "unknown"
        content = _get_content(msg)
        if isinstance(content, str):
            lines.append(f"[{role}]: {content}")
        elif isinstance(content, list):
            for block in content:
                bt = _get_block_type(block)
                if bt == "text":
                    text = _get_block_text(block)
                    if text:
                        lines.append(f"[{role}]: {text}")
                elif bt == "tool_use":
                    lines.append(f"[{role}/tool_use]: {_get_block_name(block)}(...)")
                elif bt == "tool_result":
                    lines.append(f"[{role}/tool_result]: <result>")
        else:
            lines.append(f"[{role}]: {content!s}")

    conversation_text = "\n".join(lines)
    prompt = (
        "Please provide a concise but complete summary of the following conversation "
        "segment. Preserve all key decisions, findings, file paths, error messages, "
        "and action items. The summary will replace this segment in the context window "
        "to free up space for continued conversation.\n\n"
        f"<conversation>\n{conversation_text}\n</conversation>\n\nSummary:"
    )

    try:
        if hasattr(backend, "complete"):
            return str(await backend.complete(prompt, max_tokens=max_tokens)).strip()
        elif hasattr(backend, "generate"):
            return str(await backend.generate(prompt, max_tokens=max_tokens)).strip()
        elif hasattr(backend, "chat"):
            result = await backend.chat([{"role": "user", "content": prompt}])
            if isinstance(result, dict):
                return str(result.get("content", result.get("text", ""))).strip()
            return str(result).strip()
        logger.warning("summarize_messages: backend has no known completion method")
        return ""
    except Exception as e:
        logger.warning("summarize_messages: LLM summarization failed: %s", e)
        return ""


async def compact_history(
    messages: list[Any],
    model_id: str,
    backend: Any,
    system_prompt: str = "",
    reserve_tokens: int = 4096,
    max_history_share: float = MAX_HISTORY_SHARE,
    base_chunk_ratio: float = BASE_CHUNK_RATIO,
    min_chunk_ratio: float = MIN_CHUNK_RATIO,
    fallback_keep_last: int = FALLBACK_KEEP_LAST,
) -> tuple[list[Any], bool]:
    """Compact conversation history to free context window space.

    Algorithm (mirrors openclaw's pruneHistoryForContextShare + summarizeInStages):

    Phase 1 — Drop oldest messages one-by-one, repair_tool_pairs() after EACH.
    Phase 2 — If still over budget: adaptive chunking + parallel LLM summarization.
    Phase 3 — Final repair pass.
    Fallback — Keep last N messages if all LLM summarization fails.

    Returns:
        (compacted_messages, was_compacted) tuple
    """
    if not messages:
        return messages, False

    context_window = get_context_window(model_id)
    sys_tokens = estimate_tokens(system_prompt) if system_prompt else 0
    history_budget = int(context_window * max_history_share) - sys_tokens - reserve_tokens

    if history_budget <= 0:
        logger.warning(
            "compact_history: history budget is %d, keeping last %d messages",
            history_budget,
            fallback_keep_last,
        )
        return repair_tool_pairs(messages[-fallback_keep_last:]), True

    current = list(messages)
    was_compacted = False

    # ── Phase 1: Prune oldest messages ────────────────────────────────────
    max_prune_iters = len(current) * 2
    prune_count = 0

    while prune_count < max_prune_iters:
        if estimate_messages_tokens(current) <= history_budget:
            break
        if len(current) <= 2:
            break
        current.pop(0)
        was_compacted = True
        current = repair_tool_pairs(current)  # CRITICAL after every drop
        prune_count += 1

    logger.debug(
        "compact_history: pruned %d messages, %d remain", prune_count, len(current)
    )

    # ── Phase 2: LLM summarization if still over budget ───────────────────
    if estimate_messages_tokens(current) > history_budget and len(current) > 2:
        chunk_ratio = _compute_adaptive_chunk_ratio(
            current, context_window, base_chunk_ratio, min_chunk_ratio
        )
        chunk_budget = int(context_window * chunk_ratio)

        chunks: list[list[Any]] = []
        cur_chunk: list[Any] = []
        cur_tokens = 0

        for msg in current:
            mt = estimate_messages_tokens([msg])
            if cur_tokens + mt > chunk_budget and cur_chunk:
                chunks.append(cur_chunk)
                cur_chunk = [msg]
                cur_tokens = mt
            else:
                cur_chunk.append(msg)
                cur_tokens += mt
        if cur_chunk:
            chunks.append(cur_chunk)

        logger.debug(
            "compact_history: summarizing %d chunks (ratio=%.2f, budget=%d tokens)",
            len(chunks),
            chunk_ratio,
            chunk_budget,
        )

        summaries = await asyncio.gather(
            *[summarize_messages(chunk, model_id, backend) for chunk in chunks],
            return_exceptions=True,
        )

        parts: list[str] = []
        for i, s in enumerate(summaries):
            if isinstance(s, Exception):
                logger.warning("compact_history: chunk %d failed: %s", i, s)
            elif s:
                parts.append(s)

        if parts:
            merged = "\n\n".join(parts)
            current = [_make_summary_message(merged)]
            current = repair_tool_pairs(current)
            was_compacted = True
            logger.info(
                "compact_history: summarized %d chunks → %d-token summary",
                len(chunks),
                estimate_tokens(merged),
            )
        else:
            logger.warning(
                "compact_history: all summarization failed, keeping last %d",
                fallback_keep_last,
            )
            current = repair_tool_pairs(messages[-fallback_keep_last:])
            was_compacted = True

    # ── Final repair pass ─────────────────────────────────────────────────
    current = repair_tool_pairs(current)

    logger.info(
        "compact_history: %d → %d messages, %d tokens (budget: %d)",
        len(messages),
        len(current),
        estimate_messages_tokens(current),
        history_budget,
    )
    return current, was_compacted


# ── Message duck-typing helpers ────────────────────────────────────────────────

def _get_content(msg: Any) -> Any:
    if isinstance(msg, dict):
        return msg.get("content", "")
    return getattr(msg, "content", "")


def _get_role(msg: Any) -> str:
    if isinstance(msg, dict):
        return str(msg.get("role", ""))
    return str(getattr(msg, "role", ""))


def _get_block_type(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("type", ""))
    return str(getattr(block, "type", ""))


def _get_block_id(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("id")
    return getattr(block, "id", None)


def _get_block_tool_use_id(block: Any) -> str | None:
    if isinstance(block, dict):
        return block.get("tool_use_id")
    return getattr(block, "tool_use_id", None)


def _get_block_text(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("text", ""))
    return str(getattr(block, "text", ""))


def _get_block_name(block: Any) -> str:
    if isinstance(block, dict):
        return str(block.get("name", "unknown"))
    return str(getattr(block, "name", "unknown"))


def _rebuild_message(msg: Any, new_content: list[Any]) -> Any:
    if isinstance(msg, dict):
        return {**msg, "content": new_content}
    try:
        if hasattr(msg, "model_copy"):
            return msg.model_copy(update={"content": new_content})
        if hasattr(msg, "_replace"):
            return msg._replace(content=new_content)
    except Exception:
        pass
    return {"role": _get_role(msg), "content": new_content}


def _make_summary_message(summary_text: str) -> dict[str, Any]:
    """Create a synthetic user message containing the conversation summary."""
    return {
        "role": "user",
        "content": (
            "[CONVERSATION SUMMARY — earlier context compacted to save space]\n\n"
            f"{summary_text}\n\n"
            "[END SUMMARY — conversation continues below]"
        ),
    }
