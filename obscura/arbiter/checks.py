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
    *,
    output_text: str = "",
) -> tuple[float, list[str]]:
    """Score a task that has been marked completed.

    *output_text* is the agent's output for relevance checking against
    the task subject/description.

    Returns (score, issues).
    """
    issues: list[str] = []
    score = 1.0

    # Output should be non-empty for completed tasks
    output = str(task.get("output", "") or output_text or "")
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

    # Output-task relevance: does the output relate to the task?
    subject = str(task.get("subject", "") or "")
    description = str(task.get("description", "") or "")
    task_text = f"{subject} {description}"
    task_keywords = _extract_keywords(task_text)
    if task_keywords and output.strip():
        output_keywords = _extract_keywords(output)
        if output_keywords:
            overlap = task_keywords & output_keywords
            ratio = len(overlap) / len(task_keywords)
            if ratio < 0.05:
                issues.append(
                    f"Output has {ratio:.0%} relevance to task "
                    f"'{subject[:50]}' — may be unrelated"
                )
                score -= 0.3
            elif ratio < 0.15:
                issues.append(
                    f"Low output relevance: {ratio:.0%} keyword overlap "
                    f"with task '{subject[:50]}'"
                )
                score -= 0.15

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


# ---------------------------------------------------------------------------
# Test results check
# ---------------------------------------------------------------------------


def check_test_results(
    outcome: Mapping[str, Any],
) -> tuple[float, list[str]]:
    """Score based on test runner outcome.

    *outcome* should have keys: passed, failed, errors, failed_tests,
    timeout_exceeded.

    Returns (score, issues).
    """
    issues: list[str] = []

    if outcome.get("timeout_exceeded"):
        issues.append("Test run timed out")
        return 0.5, issues

    failed = int(outcome.get("failed", 0))
    errors = int(outcome.get("errors", 0))
    passed = int(outcome.get("passed", 0))
    total = passed + failed + errors

    if total == 0:
        return 1.0, []  # No tests found — can't penalize.

    if failed == 0 and errors == 0:
        return 1.0, []  # All green.

    # Failures
    failed_names = outcome.get("failed_tests") or ()
    if failed > 0:
        names = ", ".join(str(t) for t in list(failed_names)[:3])
        issues.append(f"{failed} test(s) failed: {names}")

    if errors > 0:
        issues.append(f"{errors} test error(s)")

    # Score based on failure ratio.
    failure_ratio = (failed + errors) / total
    if failure_ratio >= 0.5:
        return 0.1, issues
    if failure_ratio >= 0.2:
        return 0.3, issues
    return 0.5, issues


# ---------------------------------------------------------------------------
# File quality check (wires eval_checks.py pipeline)
# ---------------------------------------------------------------------------


def check_file_quality(
    files_touched: Sequence[str],
    *,
    skip_pyright: bool = True,
) -> tuple[float, list[str]]:
    """Run the eval_checks.py pipeline on recently modified files.

    Delegates to the existing deterministic checkers: Python syntax,
    ruff, imports, YAML/TOML/JSON validation, shell syntax, etc.

    *skip_pyright* defaults to True because pyright has a 30s timeout
    that's too slow for per-turn checks.  Set False for task completion.

    Returns (score, issues).  Score penalty: -0.1 per file with errors,
    capped at -0.5.
    """
    import os

    issues: list[str] = []
    files_with_errors = 0

    for fpath in files_touched:
        if not os.path.isfile(fpath):
            continue

        # Determine which check to run based on extension.
        error_text = _run_file_check(fpath, skip_pyright=skip_pyright)
        if error_text:
            files_with_errors += 1
            # Truncate per-file error to avoid bloating the issues list.
            issues.append(f"{os.path.basename(fpath)}: {error_text[:120]}")

    if not files_with_errors:
        return 1.0, []

    penalty = min(0.1 * files_with_errors, 0.5)
    return max(1.0 - penalty, 0.0), issues


def _run_file_check(path: str, *, skip_pyright: bool = True) -> str:
    """Run appropriate eval checks on a single file. Returns error text or ''."""
    try:
        from obscura.core.eval_checks import (
            check_python_syntax,
            check_shell_syntax,
            check_written_json,
            check_written_yaml_toml,
        )

        tool_input = {"file_path": path, "path": path}
        errors: list[str] = []

        if path.endswith(".py"):
            # Syntax check (instant).
            r = check_python_syntax("write_file", tool_input, "")
            if r:
                errors.append(r)
            # Ruff (fast, <1s).
            try:
                from obscura.core.eval_checks import check_python_ruff

                r = check_python_ruff("write_file", tool_input, "")
                if r:
                    errors.append(r)
            except ImportError:
                pass
            # Pyright (slow, optional).
            if not skip_pyright:
                try:
                    from obscura.core.eval_checks import check_python_pyright

                    r = check_python_pyright("write_file", tool_input, "")
                    if r:
                        errors.append(r)
                except ImportError:
                    pass
        elif path.endswith((".yaml", ".yml", ".toml")):
            r = check_written_yaml_toml("write_file", tool_input, "")
            if r:
                errors.append(r)
        elif path.endswith(".json"):
            r = check_written_json("write_file", tool_input, "")
            if r:
                errors.append(r)
        elif path.endswith((".sh", ".bash")):
            r = check_shell_syntax("write_file", tool_input, "")
            if r:
                errors.append(r)

        return " | ".join(errors) if errors else ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# File change relevance
# ---------------------------------------------------------------------------


def check_file_relevance(
    task_subject: str,
    task_description: str,
    files_touched: Sequence[str],
) -> tuple[float, list[str]]:
    """Check whether modified files are relevant to the task.

    Extracts keywords from file path components and compares against
    task keywords.  Files with zero keyword overlap are flagged as
    potentially irrelevant.

    Returns (score, issues).
    """
    issues: list[str] = []
    if not files_touched:
        return 1.0, []

    task_keywords = _extract_keywords(f"{task_subject} {task_description}")
    if not task_keywords:
        return 1.0, []

    irrelevant: list[str] = []
    for fpath in set(files_touched):
        # Extract keywords from path components.
        import os

        parts = os.path.normpath(fpath).replace("\\", "/").split("/")
        # Split each part on common separators.
        path_words: set[str] = set()
        for part in parts:
            # Strip extension.
            name = part.rsplit(".", 1)[0] if "." in part else part
            tokens = re.split(r"[-_./]", name.lower())
            path_words.update(t for t in tokens if len(t) > 2 and t not in _STOP_WORDS)

        if not path_words:
            continue

        overlap = path_words & task_keywords
        if not overlap:
            irrelevant.append(os.path.basename(fpath))

    if not irrelevant:
        return 1.0, []

    total = len(set(files_touched))
    irrelevant_ratio = len(irrelevant) / total

    if irrelevant_ratio <= 0.25:
        return 1.0, []

    if irrelevant_ratio <= 0.5:
        issues.append(
            f"{len(irrelevant)}/{total} modified files appear unrelated to task: "
            f"{', '.join(irrelevant[:5])}"
        )
        return 0.6, issues

    issues.append(
        f"{len(irrelevant)}/{total} modified files unrelated to "
        f"'{task_subject[:40]}': {', '.join(irrelevant[:5])}"
    )
    return 0.3, issues


# ---------------------------------------------------------------------------
# Scope creep / unnecessary work detection
# ---------------------------------------------------------------------------

# Heuristic complexity tiers based on task description length and keywords.
_COMPLEXITY_SIGNALS_SMALL = frozenset(
    {
        "add",
        "fix",
        "typo",
        "rename",
        "bump",
        "update",
        "remove",
        "delete",
        "log",
        "print",
        "comment",
        "toggle",
        "flag",
        "env",
        "config",
    }
)
_COMPLEXITY_SIGNALS_LARGE = frozenset(
    {
        "refactor",
        "migrate",
        "rewrite",
        "redesign",
        "architect",
        "overhaul",
        "implement",
        "build",
        "create",
        "integrate",
        "system",
        "pipeline",
        "framework",
        "engine",
    }
)


def _estimate_task_complexity(subject: str, description: str) -> str:
    """Estimate task complexity as 'small', 'medium', or 'large'.

    Uses description length + keyword signals.  This is intentionally
    crude — it's a heuristic, not a judgment.
    """
    text = f"{subject} {description}".lower()
    words = set(text.split())
    total_len = len(text)

    large_hits = words & _COMPLEXITY_SIGNALS_LARGE
    small_hits = words & _COMPLEXITY_SIGNALS_SMALL

    # Strong signal: explicit large-scope keywords
    if large_hits and total_len > 80:
        return "large"
    # Strong signal: tiny task with small-scope keywords
    if small_hits and not large_hits and total_len < 60:
        return "small"

    # Fall back to description length
    if total_len > 200:
        return "large"
    if total_len < 40:
        return "small"
    return "medium"


# Expected tool-call budgets per complexity tier.
_SCOPE_BUDGETS: dict[str, dict[str, int]] = {
    "small": {"tool_calls": 10, "files_touched": 3, "turns": 4},
    "medium": {"tool_calls": 30, "files_touched": 8, "turns": 8},
    "large": {"tool_calls": 80, "files_touched": 20, "turns": 15},
}


def check_scope_creep(
    task_subject: str,
    task_description: str,
    tool_call_count: int,
    files_touched: Sequence[str],
    turn_count: int,
) -> tuple[float, list[str]]:
    """Detect gold-plating, yak-shaving, and scope creep.

    Compares the volume of agent activity against a complexity estimate
    derived from the task description.  When the agent is doing 3x more
    work than expected for a task of this size, it's flagged.

    Returns (score, issues).
    """
    issues: list[str] = []
    complexity = _estimate_task_complexity(task_subject, task_description)
    budget = _SCOPE_BUDGETS[complexity]

    unique_files = len(set(files_touched))

    # Check each dimension.  Ratio > 1.0 = over budget.
    tool_ratio = tool_call_count / budget["tool_calls"] if budget["tool_calls"] else 0
    file_ratio = (
        unique_files / budget["files_touched"] if budget["files_touched"] else 0
    )
    turn_ratio = turn_count / budget["turns"] if budget["turns"] else 0

    # Take the worst dimension.
    worst = max(tool_ratio, file_ratio, turn_ratio)

    if worst <= 1.5:
        return 1.0, []  # Within reasonable bounds.

    if worst <= 3.0:
        over_dims: list[str] = []
        if tool_ratio > 1.5:
            over_dims.append(
                f"{tool_call_count} tool calls (budget: {budget['tool_calls']})"
            )
        if file_ratio > 1.5:
            over_dims.append(
                f"{unique_files} files (budget: {budget['files_touched']})"
            )
        if turn_ratio > 1.5:
            over_dims.append(f"{turn_count} turns (budget: {budget['turns']})")
        issues.append(f"Scope creep ({complexity} task): {'; '.join(over_dims)}")
        return 0.5, issues

    # Severe: 3x+ over budget
    over_dims_severe: list[str] = []
    if tool_ratio > 1.5:
        over_dims_severe.append(f"{tool_call_count}/{budget['tool_calls']} tools")
    if file_ratio > 1.5:
        over_dims_severe.append(f"{unique_files}/{budget['files_touched']} files")
    if turn_ratio > 1.5:
        over_dims_severe.append(f"{turn_count}/{budget['turns']} turns")
    issues.append(
        f"Excessive work for {complexity} task: "
        f"{', '.join(over_dims_severe)}. "
        f"Agent may be gold-plating or yak-shaving."
    )
    return 0.2, issues


def check_retry_spiral(
    recent_errors: Sequence[str],
    *,
    similarity_threshold: float = 0.5,
) -> tuple[float, list[str]]:
    """Detect agents retrying the same failing approach with minor variations.

    Unlike the watchdog's identical-error check, this catches *similar*
    errors (e.g. different line numbers but same exception type).

    Returns (score, issues).
    """
    issues: list[str] = []
    if len(recent_errors) < 3:
        return 1.0, []

    # Compare consecutive error pairs for similarity.
    similar_pairs = 0
    for i in range(1, len(recent_errors)):
        prev_kw = _extract_keywords(recent_errors[i - 1])
        curr_kw = _extract_keywords(recent_errors[i])
        if not prev_kw or not curr_kw:
            continue
        overlap = len(prev_kw & curr_kw) / max(len(prev_kw), len(curr_kw))
        if overlap >= similarity_threshold:
            similar_pairs += 1

    similarity_ratio = similar_pairs / (len(recent_errors) - 1)

    if similarity_ratio < 0.5:
        return 1.0, []  # Errors are diverse — probably making progress.

    if similarity_ratio < 0.8:
        issues.append(
            f"Possible retry spiral: {similar_pairs}/{len(recent_errors) - 1} "
            f"consecutive error pairs are similar ({similarity_ratio:.0%})"
        )
        return 0.5, issues

    issues.append(
        f"Retry spiral: {similar_pairs}/{len(recent_errors) - 1} errors are "
        f"near-identical ({similarity_ratio:.0%}). Agent is stuck on the same "
        f"approach — needs a different strategy."
    )
    return 0.2, issues
