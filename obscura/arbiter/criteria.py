"""obscura.arbiter.criteria — Structured acceptance criteria verification.

Matches plain-text acceptance criteria against concrete evidence (test
outcomes, ruff results, filesystem state, error logs) using a cascade of
pattern-matched verifiers before falling back to keyword overlap.

Usage::

    from obscura.arbiter.criteria import verify_criterion, CriterionResult

    result = verify_criterion(
        "tests pass",
        output_text=output,
        files_changed=["obscura/core/task_queue.py"],
        test_outcome=outcome,
    )
    if not result.satisfied:
        print(result.reason)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from obscura.arbiter.test_runner import TestOutcome


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CriterionResult:
    """Result of verifying a single acceptance criterion."""

    criterion: str
    satisfied: bool
    confidence: float  # 0.0 – 1.0
    reason: str
    method: str  # "tests" | "lint" | "file_exists" | "no_errors" | "keyword"


# ---------------------------------------------------------------------------
# Pattern tables
# ---------------------------------------------------------------------------

_TESTS_PASS_PATTERNS = re.compile(
    r"\b(tests?\s+pass(es|ing)?|all\s+tests?\s+(pass|green)|pytest\s+pass(es)?|"
    r"test\s+suite\s+(pass(es)?|green)|ci\s+pass(es)?)\b",
    re.IGNORECASE,
)

_LINT_CLEAN_PATTERNS = re.compile(
    r"\b(lint\s+(clean|pass(es)?)|ruff\s+(clean|pass(es)?|ok)|"
    r"no\s+(lint|linting)\s+(errors?|issues?|warnings?)|"
    r"linting?\s+(pass(es)?|clean|ok))\b",
    re.IGNORECASE,
)

_NO_ERRORS_PATTERNS = re.compile(
    r"\b(no\s+errors?|zero\s+errors?|error[\s-]free|without\s+errors?|"
    r"no\s+(runtime|import|syntax)\s+errors?)\b",
    re.IGNORECASE,
)

_FILE_EXISTS_PATTERNS = re.compile(
    r"(?:file|path|module|script)\s+[`'\"]?([^\s`'\"]+)[`'\"]?\s+(?:exists?|created?|present)",
    re.IGNORECASE,
)

_STOP_WORDS = frozenset(
    "a an the and or but in on at to for of with is are was were be been "
    "have has had do does did will would could should may might must "
    "it its this that these those".split()
)


# ---------------------------------------------------------------------------
# Individual verifiers
# ---------------------------------------------------------------------------


def _verify_tests_pass(
    criterion: str,
    *,
    test_outcome: "TestOutcome | None" = None,
    output_text: str = "",
) -> CriterionResult | None:
    """Return a result if the criterion is about test passing, else None."""
    if not _TESTS_PASS_PATTERNS.search(criterion):
        return None

    if test_outcome is not None:
        if test_outcome.timeout_exceeded:
            return CriterionResult(
                criterion=criterion,
                satisfied=False,
                confidence=0.9,
                reason="Test suite timed out",
                method="tests",
            )
        total = test_outcome.passed + test_outcome.failed + test_outcome.errors
        if total == 0:
            # No tests found — treat as inconclusive, mild penalty.
            return CriterionResult(
                criterion=criterion,
                satisfied=False,
                confidence=0.5,
                reason="No related tests found to verify criterion",
                method="tests",
            )
        if test_outcome.failed == 0 and test_outcome.errors == 0:
            return CriterionResult(
                criterion=criterion,
                satisfied=True,
                confidence=0.95,
                reason=f"{test_outcome.passed} test(s) passed",
                method="tests",
            )
        return CriterionResult(
            criterion=criterion,
            satisfied=False,
            confidence=0.95,
            reason=(
                f"{test_outcome.failed} test(s) failed, "
                f"{test_outcome.errors} error(s): "
                f"{', '.join(test_outcome.failed_tests[:3])}"
            ),
            method="tests",
        )

    # No TestOutcome — fall back to output_text heuristic.
    lower = output_text.lower()
    if re.search(r"\d+\s+passed", lower) and "failed" not in lower:
        return CriterionResult(
            criterion=criterion,
            satisfied=True,
            confidence=0.7,
            reason="Output mentions tests passing",
            method="tests",
        )
    if re.search(r"\d+\s+failed", lower) or "error" in lower:
        return CriterionResult(
            criterion=criterion,
            satisfied=False,
            confidence=0.7,
            reason="Output mentions test failures or errors",
            method="tests",
        )
    return None  # Cannot determine — fall through to keyword.


def _verify_lint_clean(
    criterion: str,
    *,
    output_text: str = "",
    files_changed: Sequence[str] = (),
) -> CriterionResult | None:
    """Return a result if the criterion is about lint/ruff passing, else None."""
    if not _LINT_CLEAN_PATTERNS.search(criterion):
        return None

    lower = output_text.lower()

    # Strong signal: "all checks passed" (ruff output)
    if "all checks passed" in lower:
        return CriterionResult(
            criterion=criterion,
            satisfied=True,
            confidence=0.95,
            reason="Ruff reported all checks passed",
            method="lint",
        )

    # Negative signal: ruff error patterns
    if re.search(r"found \d+ error", lower) or re.search(r"[A-Z]\d{3,}", output_text):
        return CriterionResult(
            criterion=criterion,
            satisfied=False,
            confidence=0.8,
            reason="Output contains lint errors",
            method="lint",
        )

    # Try running ruff on the changed Python files directly.
    py_files = [f for f in files_changed if f.endswith(".py")]
    if py_files:
        try:
            import subprocess

            result = subprocess.run(
                ["ruff", "check", "--quiet", *py_files],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return CriterionResult(
                    criterion=criterion,
                    satisfied=True,
                    confidence=0.95,
                    reason=f"ruff check passed on {len(py_files)} file(s)",
                    method="lint",
                )
            issues = (result.stdout or result.stderr or "lint errors").strip()
            return CriterionResult(
                criterion=criterion,
                satisfied=False,
                confidence=0.95,
                reason=f"ruff check failed: {issues[:120]}",
                method="lint",
            )
        except Exception:
            pass  # ruff not available or timeout — fall through.

    return None


def _verify_file_exists(
    criterion: str,
    *,
    files_changed: Sequence[str] = (),
    output_text: str = "",
) -> CriterionResult | None:
    """Return a result if the criterion mentions a specific file must exist."""
    match = _FILE_EXISTS_PATTERNS.search(criterion)
    if not match:
        return None

    target = match.group(1).strip("/\\")
    # Check files_changed first (agent may have created it this turn).
    for f in files_changed:
        if target in f or Path(f).name == target:
            return CriterionResult(
                criterion=criterion,
                satisfied=True,
                confidence=0.9,
                reason=f"File '{target}' appears in changed files",
                method="file_exists",
            )

    # Check filesystem directly.
    if os.path.exists(target):
        return CriterionResult(
            criterion=criterion,
            satisfied=True,
            confidence=0.85,
            reason=f"File '{target}' exists on disk",
            method="file_exists",
        )

    # Check if mentioned as created in output.
    if target in output_text:
        return CriterionResult(
            criterion=criterion,
            satisfied=True,
            confidence=0.6,
            reason=f"File '{target}' mentioned in output",
            method="file_exists",
        )

    return CriterionResult(
        criterion=criterion,
        satisfied=False,
        confidence=0.7,
        reason=f"File '{target}' not found in changed files or on disk",
        method="file_exists",
    )


def _verify_no_errors(
    criterion: str,
    *,
    output_text: str = "",
    error_count: int = 0,
) -> CriterionResult | None:
    """Return a result if the criterion requires absence of errors."""
    if not _NO_ERRORS_PATTERNS.search(criterion):
        return None

    if error_count > 0:
        return CriterionResult(
            criterion=criterion,
            satisfied=False,
            confidence=0.9,
            reason=f"{error_count} error(s) recorded",
            method="no_errors",
        )

    lower = output_text.lower()
    error_signals = re.findall(
        r"\b(error|exception|traceback|failed|failure)\b", lower
    )
    if len(error_signals) > 2:  # noqa: PLR2004
        return CriterionResult(
            criterion=criterion,
            satisfied=False,
            confidence=0.7,
            reason=f"Output contains {len(error_signals)} error-related terms",
            method="no_errors",
        )

    return CriterionResult(
        criterion=criterion,
        satisfied=True,
        confidence=0.75,
        reason="No errors detected in output or error count",
        method="no_errors",
    )


def _verify_keyword_overlap(
    criterion: str,
    *,
    output_text: str = "",
    files_changed: Sequence[str] = (),
) -> CriterionResult:
    """Fallback: keyword overlap between criterion and evidence."""
    evidence = f"{output_text} {' '.join(files_changed)}"

    def _keywords(text: str) -> set[str]:
        words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", text.lower())
        return {w for w in words if w not in _STOP_WORDS}

    criterion_kw = _keywords(criterion)
    evidence_kw = _keywords(evidence)

    if not criterion_kw:
        return CriterionResult(
            criterion=criterion,
            satisfied=True,
            confidence=0.5,
            reason="Criterion has no checkable keywords",
            method="keyword",
        )

    overlap = criterion_kw & evidence_kw
    ratio = len(overlap) / len(criterion_kw)
    satisfied = ratio >= 0.3  # noqa: PLR2004

    return CriterionResult(
        criterion=criterion,
        satisfied=satisfied,
        confidence=min(0.4 + ratio * 0.4, 0.7),  # cap at 0.7 — heuristic only
        reason=(
            f"{ratio:.0%} keyword overlap ({len(overlap)}/{len(criterion_kw)} terms)"
            if satisfied
            else f"Low keyword overlap ({ratio:.0%}) — criterion may be unmet"
        ),
        method="keyword",
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_VERIFIERS = [
    _verify_tests_pass,
    _verify_lint_clean,
    _verify_file_exists,
    _verify_no_errors,
]


def verify_criterion(
    criterion: str,
    *,
    output_text: str = "",
    files_changed: Sequence[str] = (),
    test_outcome: "TestOutcome | None" = None,
    error_count: int = 0,
) -> CriterionResult:
    """Verify a single acceptance criterion against available evidence.

    Tries structured verifiers in order (tests → lint → file_exists →
    no_errors), falling back to keyword overlap for unrecognised criteria.
    """
    for verifier in _VERIFIERS:
        try:
            result = verifier(
                criterion,
                output_text=output_text,
                files_changed=files_changed,
                test_outcome=test_outcome,
                error_count=error_count,
            )
        except TypeError:
            # Verifier doesn't accept all kwargs — call with subset.
            import inspect

            sig = inspect.signature(verifier)
            params = set(sig.parameters)
            kwargs: dict = {}
            if "output_text" in params:
                kwargs["output_text"] = output_text
            if "files_changed" in params:
                kwargs["files_changed"] = files_changed
            if "test_outcome" in params:
                kwargs["test_outcome"] = test_outcome
            if "error_count" in params:
                kwargs["error_count"] = error_count
            result = verifier(criterion, **kwargs)

        if result is not None:
            return result

    return _verify_keyword_overlap(
        criterion,
        output_text=output_text,
        files_changed=files_changed,
    )


def verify_criteria(
    criteria: Sequence[str],
    *,
    output_text: str = "",
    files_changed: Sequence[str] = (),
    test_outcome: "TestOutcome | None" = None,
    error_count: int = 0,
) -> tuple[float, list[str], list[CriterionResult]]:
    """Verify all acceptance criteria. Returns (score, issues, results).

    score  — 0.0–1.0 fraction of criteria satisfied
    issues — human-readable strings for unsatisfied criteria
    results — full CriterionResult per criterion
    """
    if not criteria:
        return 1.0, [], []

    results = [
        verify_criterion(
            c,
            output_text=output_text,
            files_changed=files_changed,
            test_outcome=test_outcome,
            error_count=error_count,
        )
        for c in criteria
    ]

    satisfied = sum(1 for r in results if r.satisfied)
    score = satisfied / len(results)

    issues: list[str] = []
    if score < 1.0:
        unsatisfied = [r for r in results if not r.satisfied]
        if score < 0.5:  # noqa: PLR2004
            issues.append(
                f"Only {satisfied}/{len(results)} acceptance criteria appear met"
            )
        for r in unsatisfied:
            issues.append(f"Criterion unmet [{r.method}]: '{r.criterion[:80]}' — {r.reason}")

    return score, issues, results


__all__ = ["CriterionResult", "verify_criterion", "verify_criteria"]
