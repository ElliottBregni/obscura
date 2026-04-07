"""obscura.arbiter.checks — Deterministic check functions for the Arbiter.

Each checker returns ``(score, issues)`` where *score* is 0.0-1.0 and
*issues* is a list of human-readable strings.  An issue prefixed with
``SAFETY:`` triggers an immediate KILL verdict in the engine.

Checkers are intentionally fast (<10 ms) — they run synchronously on
every evaluation and must never block.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

# ---------------------------------------------------------------------------
# Keyword extraction (for drift detection)
# ---------------------------------------------------------------------------

_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "and",
        "but",
        "or",
        "not",
        "no",
        "if",
        "then",
        "that",
        "this",
        "it",
        "its",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "he",
        "she",
        "they",
        "them",
        "their",
        "what",
        "which",
        "who",
    }
)

# ---------------------------------------------------------------------------
# Dangerous shell patterns (SAFETY-level)
# ---------------------------------------------------------------------------

_DANGEROUS_SHELL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|--recursive)\b"),
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+.*of=/dev/"),
    re.compile(r"\b:>\s*/"),
    re.compile(r">\s*/dev/sd[a-z]"),
    re.compile(r"\bchmod\s+(-R\s+)?777\s+/"),
    re.compile(r"\bcurl\b.*\|\s*(sudo\s+)?(ba)?sh"),
)

_DANGEROUS_SQL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bDELETE\s+FROM\s+\w+\s*;", re.IGNORECASE),  # DELETE without WHERE
)


# ---------------------------------------------------------------------------
# Tool call checks
# ---------------------------------------------------------------------------


def check_tool_call(
    tool_name: str,
    args: Mapping[str, Any],
    *,
    allowlist: Sequence[str] | None = None,
    denylist: Sequence[str] | None = None,
) -> tuple[float, list[str]]:
    """Score a tool call before execution.

    Returns (score, issues).
    """
    issues: list[str] = []
    score = 1.0

    # Policy: denylist
    if denylist and tool_name in denylist:
        issues.append(f"SAFETY: tool '{tool_name}' is on the denylist")
        return 0.0, issues

    # Policy: allowlist (if set, tool must be in it)
    if allowlist and tool_name not in allowlist:
        issues.append(f"tool '{tool_name}' not in allowlist")
        score -= 0.5

    # Shell command safety
    if tool_name in ("run_shell", "bash", "shell"):
        command = str(args.get("command", "") or args.get("cmd", ""))
        for pattern in _DANGEROUS_SHELL_PATTERNS:
            if pattern.search(command):
                issues.append(f"SAFETY: dangerous shell pattern: {pattern.pattern}")
                return 0.0, issues

    # SQL safety
    raw_sql = str(
        args.get("query", "") or args.get("sql", "") or args.get("command", "")
    )
    for pattern in _DANGEROUS_SQL_PATTERNS:
        if pattern.search(raw_sql):
            issues.append(f"SAFETY: dangerous SQL pattern: {pattern.pattern}")
            return 0.0, issues

    # Empty required args
    if not args:
        issues.append("tool called with no arguments")
        score -= 0.2

    return max(score, 0.0), issues


# ---------------------------------------------------------------------------
# Model turn checks
# ---------------------------------------------------------------------------


def check_model_turn(
    output_text: str,
    *,
    tool_error_count: int = 0,
    repeated_errors: int = 0,
    lint_errors: Mapping[str, str] | None = None,
) -> tuple[float, list[str]]:
    """Score agent output after a model turn.

    Returns (score, issues).
    """
    issues: list[str] = []
    score = 1.0

    # Empty output
    if not output_text or not output_text.strip():
        issues.append("model produced empty output")
        score -= 0.3

    # Tool errors this turn
    if tool_error_count > 0:
        issues.append(f"{tool_error_count} tool error(s) this turn")
        score -= min(0.2 * tool_error_count, 0.6)

    # Repeated identical errors (spinning)
    if repeated_errors >= 3:  # noqa: PLR2004
        issues.append(f"agent appears stuck: {repeated_errors} repeated errors")
        score -= 0.4

    # Lint errors in changed files
    if lint_errors:
        total = sum(len(diag.strip().splitlines()) for diag in lint_errors.values())
        issues.append(f"{total} lint error(s) in {len(lint_errors)} file(s)")
        score -= 0.3

    return max(score, 0.0), issues


# ---------------------------------------------------------------------------
# Task completion checks
# ---------------------------------------------------------------------------


def check_task_complete(
    task: Mapping[str, Any],
) -> tuple[float, list[str]]:
    """Score a task that has been marked completed.

    Returns (score, issues).
    """
    issues: list[str] = []
    score = 1.0

    # Output should be non-empty for completed tasks
    output = str(task.get("output", "") or "")
    if not output.strip():
        issues.append("task completed with no output")
        score -= 0.3

    # Error field should be empty
    error = str(task.get("error", "") or "")
    if error.strip():
        issues.append(f"task completed but has error: {error[:100]}")
        score -= 0.3

    # Excessive retries suggest fragile execution
    retry_count = int(task.get("retry_count", 0) or 0)
    max_retries = int(task.get("max_retries", 3) or 3)
    if retry_count > 0 and max_retries > 0:
        retry_ratio = retry_count / max_retries
        if retry_ratio >= 0.5:
            issues.append(f"task used {retry_count}/{max_retries} retries")
            score -= 0.2 * retry_ratio

    return max(score, 0.0), issues


# ---------------------------------------------------------------------------
# Goal transition checks
# ---------------------------------------------------------------------------


def check_goal_transition(
    goal: Mapping[str, Any],
    *,
    linked_task_statuses: Sequence[str] | None = None,
) -> tuple[float, list[str]]:
    """Score a goal status transition.

    *linked_task_statuses* should be the status of each task linked
    to the goal (e.g. ``["completed", "completed", "pending"]``).

    Returns (score, issues).
    """
    issues: list[str] = []
    score = 1.0
    new_status = str(goal.get("status", ""))

    if new_status == "completed":
        # All linked tasks should be completed
        if linked_task_statuses:
            incomplete = [s for s in linked_task_statuses if s != "completed"]
            if incomplete:
                issues.append(
                    f"goal completing with {len(incomplete)} "
                    f"incomplete task(s) of {len(linked_task_statuses)}"
                )
                score -= 0.4

        # Progress should be 100
        progress = int(goal.get("progress", 0) or 0)
        if progress < 100:  # noqa: PLR2004
            issues.append(f"goal completing at {progress}% (expected 100%)")
            score -= 0.2

        # Should have acceptance criteria satisfied (basic presence check)
        criteria = goal.get("acceptance_criteria") or []
        if criteria and not linked_task_statuses:
            issues.append("goal has acceptance criteria but no linked tasks")
            score -= 0.2

    return max(score, 0.0), issues


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


def _extract_keywords(text: str) -> set[str]:
    """Extract meaningful keywords from text (lowercase, no stop words)."""
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text.lower())
    return {w for w in words if w not in _STOP_WORDS}


def check_drift(
    task_subject: str,
    task_description: str,
    recent_tool_calls: Sequence[str],
    recent_output: str,
) -> tuple[float, list[str]]:
    """Score whether the agent is drifting from its assigned task.

    Compares keyword overlap between the task definition and the agent's
    recent activity. Low overlap = drift.

    Returns (score, issues).
    """
    issues: list[str] = []

    # Build task keyword set.
    task_text = f"{task_subject} {task_description}"
    task_keywords = _extract_keywords(task_text)
    if not task_keywords:
        return 1.0, []  # No task context to compare against.

    # Build activity keyword set from tool calls + output.
    activity_text = " ".join(recent_tool_calls) + " " + recent_output
    activity_keywords = _extract_keywords(activity_text)
    if not activity_keywords:
        return 1.0, []  # No activity yet.

    # Keyword overlap ratio.
    overlap = task_keywords & activity_keywords
    overlap_ratio = len(overlap) / len(task_keywords) if task_keywords else 1.0

    if overlap_ratio >= 0.3:
        return 1.0, []  # Enough overlap — on track.

    if overlap_ratio >= 0.1:
        issues.append(
            f"Low task relevance: {len(overlap)}/{len(task_keywords)} "
            f"keywords overlap ({overlap_ratio:.0%})"
        )
        return 0.5, issues

    issues.append(
        f"Drift detected: agent activity has {overlap_ratio:.0%} overlap "
        f"with task '{task_subject[:50]}'"
    )
    return 0.2, issues


# ---------------------------------------------------------------------------
# Token budget check
# ---------------------------------------------------------------------------


def check_token_budget(
    tokens_used: int,
    token_budget: int,
    progress_pct: float,
) -> tuple[float, list[str]]:
    """Score token efficiency relative to task progress.

    If the agent has burned most of its budget with little progress,
    flag it.

    Returns (score, issues).
    """
    issues: list[str] = []
    if token_budget <= 0:
        return 1.0, []  # No budget constraint.

    usage_pct = tokens_used / token_budget
    if usage_pct < 0.5:
        return 1.0, []  # Plenty of budget left.

    # Compare usage to progress.
    if progress_pct <= 0:
        progress_pct = 0.01  # Avoid division by zero.

    efficiency = progress_pct / usage_pct  # >1 = ahead, <1 = behind

    if efficiency >= 0.5:
        return 1.0, []

    if efficiency >= 0.25:
        issues.append(
            f"Token burn rate high: {usage_pct:.0%} budget used, "
            f"{progress_pct:.0%} progress"
        )
        return 0.6, issues

    issues.append(
        f"Token budget critical: {usage_pct:.0%} budget used with only "
        f"{progress_pct:.0%} progress (efficiency={efficiency:.2f})"
    )
    return 0.3, issues
