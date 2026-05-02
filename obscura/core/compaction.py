"""obscura.core.compaction — Conversation history compaction and summarization.

Port of openclaw's compaction.ts + pruneHistoryForContextShare(), enhanced
with Claude Code-style multi-strategy compaction.

Provides:
- repair_tool_pairs(messages): Remove orphaned tool_result blocks after pruning
- compact_history(messages, model_id, backend): Full compaction pipeline
- summarize_messages(messages, model_id, backend): LLM-based summarization
- snip_tool_outputs(messages, threshold): Truncate verbose tool results in-place
- microcompact(messages, model_id, backend): Insert synthetic boundary summaries
- tiered_compact(messages, model_id, backend): Auto-select compaction strategy

Compaction strategies (lightest to heaviest):
1. **Snip compact** — truncate individual tool outputs above a size threshold
2. **Microcompact** — insert synthetic summary boundaries at topic transitions
3. **Full compact** — drop oldest messages + LLM summarization (existing)

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
from typing import Any, cast

from obscura.core.context_window import (
    SNIP_TOOL_OUTPUT_THRESHOLD,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_tokens,
    get_compact_thresholds,
    get_context_window,
    truncate_to_token_budget,
)

logger = logging.getLogger(__name__)

# Mirrors openclaw's compaction.ts constants
BASE_CHUNK_RATIO = 0.4  # 40% of context budget per chunk
MIN_CHUNK_RATIO = 0.15  # Floor ratio for very large messages
MAX_HISTORY_SHARE = 0.5  # Max 50% of context window for history
FALLBACK_KEEP_LAST = 20  # Messages to keep if summarization fails
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
            for block in cast(list[Any], content):
                if _get_block_type(block) == "tool_use":
                    tid = _get_block_id(block)
                    if tid:
                        tool_use_ids.add(tid)

    cleaned: list[Any] = []
    for msg in messages:
        content = _get_content(msg)
        role = _get_role(msg)

        if isinstance(content, list):
            new_blocks: list[Any] = []
            for block in cast(list[Any], content):
                if _get_block_type(block) == "tool_result":
                    tid = _get_block_tool_use_id(block)
                    if tid and tid not in tool_use_ids:
                        logger.debug(
                            "repair_tool_pairs: removing orphaned tool_result %s",
                            tid,
                        )
                        continue
                new_blocks.append(block)

            if not new_blocks:
                logger.debug(
                    "repair_tool_pairs: dropping empty %s message after repair",
                    role,
                )
                continue

            cleaned.append(_rebuild_message(msg, new_blocks))
        else:
            cleaned.append(msg)

    return cleaned


# ---------------------------------------------------------------------------
# Snip compact — truncate verbose tool outputs independently
# ---------------------------------------------------------------------------


def snip_tool_outputs(
    messages: list[Any],
    threshold_tokens: int = SNIP_TOOL_OUTPUT_THRESHOLD,
) -> tuple[list[Any], int, int]:
    """Truncate individual tool_result blocks that exceed a token threshold.

    This is the lightest compaction strategy -- it doesn't remove messages or
    change conversation structure. It only shortens oversized tool outputs,
    inserting a ``[snipped]`` marker with the original and truncated sizes.

    Args:
        messages: Message list (modified in-place via rebuild).
        threshold_tokens: Max tokens per tool_result block before snipping.

    Returns:
        (messages, snipped_count, tokens_freed) tuple.
    """
    snipped_count = 0
    tokens_freed = 0
    result: list[Any] = []

    for msg in messages:
        content = _get_content(msg)

        if not isinstance(content, list):
            result.append(msg)
            continue

        new_blocks: list[Any] = []
        msg_changed = False

        for block in cast(list[Any], content):
            if _get_block_type(block) == "tool_result":
                text = _get_block_text(block)
                if text:
                    tok = estimate_tokens(text)
                    if tok > threshold_tokens:
                        truncated, _ = truncate_to_token_budget(text, threshold_tokens)
                        freed = tok - estimate_tokens(truncated)
                        tokens_freed += freed
                        snipped_count += 1
                        snip_marker = (
                            f"\n\n[snipped: {tok:,} -> {tok - freed:,} tokens]"
                        )
                        new_block = _rebuild_block_text(block, truncated + snip_marker)
                        new_blocks.append(new_block)
                        msg_changed = True
                        logger.debug(
                            "snip_tool_outputs: snipped tool_result "
                            "%s (%d -> %d tokens)",
                            _get_block_tool_use_id(block) or "?",
                            tok,
                            tok - freed,
                        )
                        continue

            new_blocks.append(block)

        if msg_changed:
            result.append(_rebuild_message(msg, new_blocks))
        else:
            result.append(msg)

    if snipped_count:
        logger.info(
            "snip_tool_outputs: snipped %d tool outputs, freed ~%d tokens",
            snipped_count,
            tokens_freed,
        )

    return result, snipped_count, tokens_freed


def snip_single_output(
    text: str,
    threshold_tokens: int = SNIP_TOOL_OUTPUT_THRESHOLD,
) -> str:
    """Snip a single tool output string if it exceeds the threshold.

    Returns the (possibly truncated) string. Used by the agent loop for
    reactive mid-turn snipping of individual tool results before they
    enter the message history.
    """
    if not text:
        return text
    tok = estimate_tokens(text)
    if tok <= threshold_tokens:
        return text
    truncated, _ = truncate_to_token_budget(text, threshold_tokens)
    freed = tok - estimate_tokens(truncated)
    return truncated + f"\n\n[snipped: {tok:,} -> {tok - freed:,} tokens]"


# ---------------------------------------------------------------------------
# Microcompact — synthetic summary boundaries
# ---------------------------------------------------------------------------


async def microcompact(
    messages: list[Any],
    model_id: str,
    backend: Any,
    preserve_recent: int = 6,
) -> tuple[list[Any], bool, int]:
    """Insert synthetic summary boundaries at natural conversation breaks.

    Instead of truncating or dropping messages, microcompact identifies
    groups of older messages and replaces each group with a brief synthetic
    summary message. Recent messages (last ``preserve_recent`` pairs) are
    always kept intact.

    This is a medium-weight strategy -- heavier than snip, lighter than
    full compaction. It preserves conversation structure while freeing
    significant space.

    Args:
        messages: Current message history.
        model_id: Model ID for token estimation.
        backend: LLM backend for summarization.
        preserve_recent: Number of recent message pairs to preserve.

    Returns:
        (compacted_messages, was_compacted, tokens_freed) tuple.
    """
    if not messages:
        return messages, False, 0

    # Preserve the most recent messages
    preserve_count = min(preserve_recent * 2, len(messages))
    if len(messages) <= preserve_count + 2:
        return messages, False, 0

    keep = messages[-preserve_count:]
    older = messages[:-preserve_count]

    if not older:
        return messages, False, 0

    tokens_before = estimate_messages_tokens(older)

    # Split older messages into segments at natural boundaries.
    segments = _split_at_boundaries(older, min_segment_size=4)

    if len(segments) <= 1 and len(older) <= 6:
        return messages, False, 0

    # Summarize each segment in parallel
    summaries = await asyncio.gather(
        *[
            summarize_messages(seg, model_id, backend, max_tokens=512)
            for seg in segments
        ],
        return_exceptions=True,
    )

    compacted: list[Any] = []
    for seg, summary in zip(segments, summaries):
        if isinstance(summary, str) and summary:
            compacted.append(_make_microcompact_boundary(summary, len(seg)))
        else:
            compacted.extend(seg)

    compacted.extend(keep)
    compacted = repair_tool_pairs(compacted)

    tokens_after = estimate_messages_tokens(compacted)
    tokens_freed = tokens_before - (tokens_after - estimate_messages_tokens(keep))

    logger.info(
        "microcompact: %d segments summarized, %d -> %d messages, ~%d tokens freed",
        len(segments),
        len(messages),
        len(compacted),
        max(0, tokens_freed),
    )

    return compacted, True, max(0, tokens_freed)


def _split_at_boundaries(
    messages: list[Any],
    min_segment_size: int = 4,
) -> list[list[Any]]:
    """Split messages into segments at natural conversation boundaries.

    A boundary is detected when a user message follows an assistant message
    and the user message content looks like a new topic (not a tool result).
    """
    if len(messages) <= min_segment_size:
        return [messages]

    segments: list[list[Any]] = []
    current: list[Any] = []
    prev_role = ""

    for msg in messages:
        role = _get_role(msg)
        content = _get_content(msg)

        # Detect boundary: user message after assistant, not a tool result
        is_boundary = (
            role == "user"
            and prev_role == "assistant"
            and isinstance(content, str)
            and len(current) >= min_segment_size
        )

        if is_boundary:
            segments.append(current)
            current = []

        current.append(msg)
        prev_role = role

    if current:
        segments.append(current)

    return segments


def _make_microcompact_boundary(
    summary_text: str, original_count: int
) -> dict[str, Any]:
    """Create a synthetic boundary message for microcompact."""
    return {
        "role": "user",
        "content": (
            f"[CONTEXT BOUNDARY -- {original_count} messages summarized]\n\n"
            f"{summary_text}\n\n"
            "[END BOUNDARY]"
        ),
    }


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
            for block in cast(list[Any], content):
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
        if hasattr(backend, "generate"):
            return str(await backend.generate(prompt, max_tokens=max_tokens)).strip()
        if hasattr(backend, "chat"):
            result = await backend.chat([{"role": "user", "content": prompt}])
            if isinstance(result, dict):
                d = cast(dict[str, Any], result)
                return str(d.get("content", d.get("text", ""))).strip()
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
) -> tuple[list[Any], bool, list[dict[str, str]]]:
    """Compact conversation history to free context window space.

    Algorithm (mirrors openclaw's pruneHistoryForContextShare + summarizeInStages):

    Phase 1 -- Drop oldest messages one-by-one, repair_tool_pairs() after EACH.
    Phase 2 -- If still over budget: adaptive chunking + parallel LLM summarization.
    Phase 3 -- Final repair pass.
    Fallback -- Keep last N messages if all LLM summarization fails.

    Returns:
        (compacted_messages, was_compacted, extracted_memories) tuple.

    """
    if not messages:
        return messages, False, []

    context_window = get_context_window(model_id)
    sys_tokens = estimate_tokens(system_prompt) if system_prompt else 0
    history_budget = (
        int(context_window * max_history_share) - sys_tokens - reserve_tokens
    )

    if history_budget <= 0:
        logger.warning(
            "compact_history: history budget is %d, keeping last %d messages",
            history_budget,
            fallback_keep_last,
        )
        return repair_tool_pairs(messages[-fallback_keep_last:]), True, []

    current = list(messages)
    was_compacted = False

    # -- Phase 1: Prune oldest messages ------------------------------------
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
        "compact_history: pruned %d messages, %d remain",
        prune_count,
        len(current),
    )

    # -- Phase 2: LLM summarization if still over budget -------------------
    if estimate_messages_tokens(current) > history_budget and len(current) > 2:
        chunk_ratio = _compute_adaptive_chunk_ratio(
            current,
            context_window,
            base_chunk_ratio,
            min_chunk_ratio,
        )
        chunk_budget = int(context_window * chunk_ratio)

        chunks: list[list[Any]] = []
        cur_chunk: list[Any] = []
        cur_tokens = 0

        for msg in current:
            mt = estimate_message_tokens(msg)
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
            elif isinstance(s, str) and s:
                parts.append(s)

        if parts:
            merged = "\n\n".join(parts)
            current = [_make_summary_message(merged)]
            current = repair_tool_pairs(current)
            was_compacted = True
            logger.info(
                "compact_history: summarized %d chunks -> %d-token summary",
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

    # -- Final repair pass -------------------------------------------------
    current = repair_tool_pairs(current)

    logger.info(
        "compact_history: %d -> %d messages, %d tokens (budget: %d)",
        len(messages),
        len(current),
        estimate_messages_tokens(current),
        history_budget,
    )

    # -- Memory extraction from pruned messages ----------------------------
    extracted_memories: list[dict[str, str]] = []
    if was_compacted and prune_count > 0:
        dropped = messages[:prune_count]
        try:
            extracted_memories = await extract_memories(dropped, model_id, backend)
            if extracted_memories:
                logger.info(
                    "compact_history: extracted %d memories from %d pruned messages",
                    len(extracted_memories),
                    len(dropped),
                )
        except Exception:
            logger.debug("compact_history: memory extraction failed", exc_info=True)

    return current, was_compacted, extracted_memories


async def extract_memories(
    messages: list[Any],
    model_id: str,
    backend: Any,
) -> list[dict[str, str]]:
    """Extract key facts/learnings from messages before they are discarded.

    Sends the about-to-be-pruned conversation segment to the LLM with
    a memory extraction prompt. Returns a list of {"key": ..., "value": ...}
    entries suitable for storage in MemoryStore.

    Pattern from claude-code's ``extractMemories`` service.
    """
    if not messages:
        return []

    lines: list[str] = []
    for msg in messages:
        role = _get_role(msg) or "unknown"
        content = _get_content(msg)
        if isinstance(content, str):
            lines.append(f"[{role}]: {content[:500]}")
        elif isinstance(content, list):
            for block in cast(list[Any], content):
                if _get_block_type(block) == "text":
                    text = _get_block_text(block)
                    if text:
                        lines.append(f"[{role}]: {text[:500]}")

    if not lines:
        return []

    conversation_text = "\n".join(lines)
    if len(conversation_text) > 10_000:
        conversation_text = conversation_text[:10_000] + "\n...[truncated]"

    prompt = (
        "Extract key facts, decisions, and learnings from this conversation segment "
        "that should be remembered for future sessions. Return a JSON array of objects "
        "with 'key' and 'value' fields. Only include genuinely useful information -- "
        "file paths, architectural decisions, user preferences, error resolutions, etc.\n\n"
        "Example output:\n"
        '[{"key": "auth_approach", "value": "Using JWT with 24h expiry, refresh tokens in httpOnly cookies"},\n'
        ' {"key": "user_pref_testing", "value": "User prefers integration tests over unit tests for DB code"}]\n\n'
        "If there's nothing worth remembering, return an empty array: []\n\n"
        f"<conversation>\n{conversation_text}\n</conversation>\n\n"
        "JSON array:"
    )

    try:
        result_text = ""
        if hasattr(backend, "complete"):
            result_text = str(await backend.complete(prompt, max_tokens=1024)).strip()
        elif hasattr(backend, "generate"):
            result_text = str(await backend.generate(prompt, max_tokens=1024)).strip()
        elif hasattr(backend, "chat"):
            result = await backend.chat([{"role": "user", "content": prompt}])
            if isinstance(result, dict):
                d = cast(dict[str, Any], result)
                result_text = str(d.get("content", d.get("text", ""))).strip()
            else:
                result_text = str(result).strip()

        if not result_text:
            return []

        import json
        import re

        cleaned = re.sub(r"```(?:json)?\s*", "", result_text).strip()
        cleaned = cleaned.rstrip("`").strip()
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            results: list[dict[str, str]] = []
            for raw in cast(list[Any], parsed):
                if not isinstance(raw, dict):
                    continue
                m = cast(dict[str, Any], raw)
                if m.get("key") and m.get("value"):
                    results.append(
                        {"key": str(m.get("key", "")), "value": str(m.get("value", ""))}
                    )
            return results
        return []
    except Exception:
        logger.debug("extract_memories: failed to parse response", exc_info=True)
        return []


def should_auto_compact(
    messages: list[Any],
    model_id: str,
    system_prompt: str = "",
    threshold: float = 0.80,
) -> bool:
    """Check if auto-compaction should trigger.

    Returns True if token usage exceeds *threshold* (default 80%) of the
    context window.
    """
    if not messages:
        return False
    context_window = get_context_window(model_id)
    sys_tokens = estimate_tokens(system_prompt) if system_prompt else 0
    msg_tokens = estimate_messages_tokens(messages)
    used = sys_tokens + msg_tokens
    usage_ratio = used / context_window if context_window > 0 else 0.0
    return usage_ratio >= threshold


def evaluate_compact_tier(
    messages: list[Any],
    model_id: str,
    system_prompt: str = "",
) -> str:
    """Return the compaction tier based on current token usage.

    Returns one of: "ok", "snip", "compact", "critical".
    Uses model-specific thresholds from ``get_compact_thresholds()``.
    """
    if not messages:
        return "ok"
    thresholds = get_compact_thresholds(model_id)
    sys_tokens = estimate_tokens(system_prompt) if system_prompt else 0
    msg_tokens = estimate_messages_tokens(messages)
    return thresholds.usage_tier(sys_tokens + msg_tokens)


async def tiered_compact(
    messages: list[Any],
    model_id: str,
    backend: Any,
    system_prompt: str = "",
    reserve_tokens: int = 4096,
) -> tuple[list[Any], str, int]:
    """Run the appropriate compaction strategy based on current token usage.

    Progressively applies compaction strategies from lightest to heaviest
    until usage drops below the compact threshold:

    1. "snip"     -> snip_tool_outputs()
    2. "compact"  -> microcompact() then compact_history() if needed
    3. "critical" -> aggressive compact_history() with smaller preserve window

    Args:
        messages: Current message history.
        model_id: Model ID for thresholds and token estimation.
        backend: LLM backend for summarization.
        system_prompt: System prompt (counted toward usage).
        reserve_tokens: Response reserve.

    Returns:
        (compacted_messages, strategy_used, tokens_freed) tuple.
        strategy_used is one of: "none", "snip", "microcompact",
        "compact", "critical".
    """
    thresholds = get_compact_thresholds(model_id)
    sys_tokens = estimate_tokens(system_prompt) if system_prompt else 0

    def _used() -> int:
        return sys_tokens + estimate_messages_tokens(messages)

    tier = thresholds.usage_tier(_used())
    if tier == "ok":
        return messages, "none", 0

    tokens_before = _used()

    # Strategy 1: Snip verbose tool outputs
    if tier in ("snip", "compact", "critical"):
        messages, snipped, _ = snip_tool_outputs(messages)
        if snipped and thresholds.usage_tier(_used()) == "ok":
            return messages, "snip", tokens_before - _used()

    # Strategy 2: Microcompact (synthetic boundaries)
    if tier in ("compact", "critical"):
        preserve = thresholds.preserve_recent
        messages, did_compact, _ = await microcompact(
            messages, model_id, backend, preserve_recent=preserve
        )
        if did_compact and thresholds.usage_tier(_used()) == "ok":
            return messages, "microcompact", tokens_before - _used()

    # Strategy 3: Full compaction
    if tier in ("compact", "critical"):
        preserve = thresholds.preserve_recent
        if tier == "critical":
            preserve = max(2, preserve // 2)
            history_share = 0.35
        else:
            history_share = MAX_HISTORY_SHARE

        messages, did_compact, _memories = await compact_history(
            messages,
            model_id,
            backend,
            system_prompt=system_prompt,
            reserve_tokens=reserve_tokens,
            max_history_share=history_share,
            fallback_keep_last=preserve * 2,
        )
        strategy = "critical" if tier == "critical" else "compact"
        return messages, strategy, tokens_before - _used()

    return messages, "none", 0


# -- Message duck-typing helpers -------------------------------------------


def _get_content(msg: Any) -> Any:
    if isinstance(msg, dict):
        return cast(dict[str, Any], msg).get("content", "")
    return getattr(msg, "content", "")


def _get_role(msg: Any) -> str:
    if isinstance(msg, dict):
        return str(cast(dict[str, Any], msg).get("role", ""))
    return str(getattr(msg, "role", ""))


def _get_block_type(block: Any) -> str:
    if isinstance(block, dict):
        return str(cast(dict[str, Any], block).get("type", ""))
    return str(getattr(block, "type", ""))


def _get_block_id(block: Any) -> str | None:
    if isinstance(block, dict):
        result = cast(dict[str, Any], block).get("id")
        return result if isinstance(result, str) else None
    val = getattr(block, "id", None)
    return val if isinstance(val, str) else None


def _get_block_tool_use_id(block: Any) -> str | None:
    if isinstance(block, dict):
        result = cast(dict[str, Any], block).get("tool_use_id")
        return result if isinstance(result, str) else None
    val = getattr(block, "tool_use_id", None)
    return val if isinstance(val, str) else None


def _get_block_text(block: Any) -> str:
    if isinstance(block, dict):
        return str(cast(dict[str, Any], block).get("text", ""))
    return str(getattr(block, "text", ""))


def _get_block_name(block: Any) -> str:
    if isinstance(block, dict):
        return str(cast(dict[str, Any], block).get("name", "unknown"))
    return str(getattr(block, "name", "unknown"))


def _rebuild_block_text(block: Any, new_text: str) -> Any:
    """Rebuild a content block with new text, preserving other fields."""
    if isinstance(block, dict):
        return {**cast(dict[str, Any], block), "text": new_text}
    try:
        if hasattr(block, "model_copy"):
            return block.model_copy(update={"text": new_text})
        if hasattr(block, "_replace"):
            return block._replace(text=new_text)
    except Exception:
        pass
    result: dict[str, Any] = {"type": _get_block_type(block), "text": new_text}
    tid = _get_block_tool_use_id(block)
    if tid:
        result["tool_use_id"] = tid
    return result


def _rebuild_message(msg: Any, new_content: list[Any]) -> Any:
    if isinstance(msg, dict):
        return {**cast(dict[str, Any], msg), "content": new_content}
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
            "[CONVERSATION SUMMARY -- earlier context compacted to save space]\n\n"
            f"{summary_text}\n\n"
            "[END SUMMARY -- conversation continues below]"
        ),
    }
