"""obscura.arbiter.judge — LLM-as-judge wrapper for the Arbiter.

Delegates to the existing ``eval/scoring.py`` infrastructure but adds
budget-gating, ambiguity-triggered invocation, and session-scoped call
counting.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from obscura.core.auth import resolve_auth
from obscura.core.types import Backend
from obscura.eval.eval_backend import AnthropicEvalBackend

if TYPE_CHECKING:
    from obscura.arbiter.types import ArbiterCheckKind, ArbiterConfig

logger = logging.getLogger(__name__)

_JUDGE_PROMPT = """You are evaluating an AI agent action for quality and correctness.

## Action Type
{check_kind}

## Context
{context_summary}

## Agent Output / Action
{output_text}

## Issues Found by Automated Checks
{issues_text}

## Instructions
Rate this action 1-5:
  1 = Critical failure, dangerous or completely wrong
  2 = Poor quality, significant problems
  3 = Acceptable but flawed
  4 = Good, minor issues at most
  5 = Excellent, no issues

Respond with ONLY a JSON object: {{"score": <int 1-5>, "reasoning": "<explanation>", "suggested_fix": "<optional one-line fix suggestion or empty>"}}"""


async def maybe_judge(
    deterministic_score: float,
    check_kind: ArbiterCheckKind,
    context: dict[str, Any],
    config: ArbiterConfig,
    *,
    session_judge_calls: int = 0,
    issues: list[str] | None = None,
) -> tuple[float | None, str]:
    """Optionally invoke the LLM judge based on config and score ambiguity.

    Returns ``(normalized_score, reasoning)`` or ``(None, "")`` if the
    judge was not invoked.  The normalized score is 0.0-1.0.
    """
    # Check if judge should run.
    if config.judge_mode == "never":
        return None, ""

    if session_judge_calls >= config.max_judge_calls_per_session:
        logger.debug("Judge budget exhausted (%d calls)", session_judge_calls)
        return None, "budget_exhausted"

    # High-stakes checks always get judged (budget permitting).
    _HIGH_STAKES = {"task_complete", "goal_transition"}

    if config.judge_mode == "on_ambiguity":
        if str(check_kind) in _HIGH_STAKES:
            pass  # Always invoke for high-stakes decisions.
        elif deterministic_score >= 0.8 or deterministic_score < 0.3:  # noqa: PLR2004
            return None, ""

    # Build prompt.
    issues_text = "\n".join(f"- {i}" for i in (issues or [])) or "(none)"
    output_text = str(context.get("output_text", ""))[:2000]
    context_summary = str(context.get("summary", ""))[:500]

    prompt = _JUDGE_PROMPT.format(
        check_kind=str(check_kind),
        context_summary=context_summary or "(no context)",
        output_text=output_text or "(no output)",
        issues_text=issues_text,
    )

    # Try to get a judge backend.
    try:
        backend = _get_judge_backend()
        if backend is None:
            return None, "no_backend"
    except Exception:
        logger.debug("Could not get judge backend", exc_info=True)
        return None, "backend_error"

    # Call the judge.
    try:
        message = await backend.send(prompt)
        response_text = ""
        for block in message.content:
            if block.kind == "text":
                response_text += block.text

        parsed = json.loads(response_text)
        raw_score = float(parsed.get("score", 3))
        reasoning = str(parsed.get("reasoning", ""))
        # Normalize 1-5 Likert to 0.0-1.0.
        normalized = (max(1.0, min(5.0, raw_score)) - 1.0) / 4.0
        return normalized, reasoning
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        logger.warning("Judge response parse failed: %s", exc)
        return None, f"parse_error: {exc}"
    except Exception as exc:
        logger.warning("Judge call failed: %s", exc)
        return None, f"error: {exc}"


def _get_judge_backend() -> Any:
    """Best-effort: get a backend for judge calls.

    Returns a minimal Anthropic-API backend pinned to Haiku for cost.
    ``None`` if no Anthropic credentials are available.
    """
    import os

    try:
        auth = resolve_auth(Backend.CLAUDE)
        api_key = auth.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return None
        return AnthropicEvalBackend(
            api_key=api_key,
            model="claude-haiku-4-5-20251001",
        )
    except Exception:
        logger.debug("suppressed exception in _get_judge_backend", exc_info=True)
        return None
