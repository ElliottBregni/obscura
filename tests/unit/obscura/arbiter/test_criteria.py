"""Tests for obscura.arbiter.criteria — acceptance criteria verification."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from obscura.arbiter.criteria import (
    CriterionResult,
    verify_criteria,
    verify_criterion,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _outcome(passed: int = 0, failed: int = 0, errors: int = 0, timeout: bool = False):
    """Build a minimal TestOutcome-like object."""
    o = MagicMock()
    o.passed = passed
    o.failed = failed
    o.errors = errors
    o.timeout_exceeded = timeout
    o.failed_tests = [f"test_failure_{i}" for i in range(failed)]
    return o


# ---------------------------------------------------------------------------
# _verify_tests_pass
# ---------------------------------------------------------------------------


class TestVerifyTestsPass:
    def test_all_pass_via_outcome(self):
        result = verify_criterion("tests pass", test_outcome=_outcome(passed=42))
        assert result.satisfied
        assert result.confidence >= 0.9
        assert result.method == "tests"

    def test_failures_via_outcome(self):
        result = verify_criterion(
            "all tests pass", test_outcome=_outcome(passed=10, failed=2)
        )
        assert not result.satisfied
        assert result.method == "tests"

    def test_timeout_not_satisfied(self):
        result = verify_criterion("tests pass", test_outcome=_outcome(timeout=True))
        assert not result.satisfied
        assert "timed out" in result.reason.lower()

    def test_no_tests_found(self):
        result = verify_criterion("tests pass", test_outcome=_outcome())
        assert not result.satisfied
        assert result.confidence == 0.5

    def test_output_text_fallback_pass(self):
        result = verify_criterion(
            "tests pass",
            output_text="42 passed in 1.2s",
        )
        assert result.satisfied
        assert result.method == "tests"

    def test_output_text_fallback_fail(self):
        result = verify_criterion(
            "tests pass",
            output_text="3 failed, 10 passed",
        )
        assert not result.satisfied

    def test_criterion_not_about_tests_returns_none_internally(self):
        # Should fall through to keyword verifier, not tests verifier.
        result = verify_criterion("deploy the service", output_text="deployed")
        assert result.method != "tests"

    def test_errors_via_outcome(self):
        result = verify_criterion(
            "test suite passes", test_outcome=_outcome(passed=5, errors=1)
        )
        assert not result.satisfied


# ---------------------------------------------------------------------------
# _verify_lint_clean
# ---------------------------------------------------------------------------


class TestVerifyLintClean:
    def test_ruff_all_checks_passed(self):
        result = verify_criterion(
            "lint clean", output_text="All checks passed."
        )
        assert result.satisfied
        assert result.method == "lint"
        assert result.confidence >= 0.9

    def test_ruff_errors_in_output(self):
        result = verify_criterion(
            "ruff passes",
            output_text="Found 3 errors. [E501, W291, F401]",
        )
        assert not result.satisfied
        assert result.method == "lint"

    def test_not_lint_criterion(self):
        result = verify_criterion("write unit tests", output_text="tests written")
        assert result.method != "lint"


# ---------------------------------------------------------------------------
# _verify_file_exists
# ---------------------------------------------------------------------------


class TestVerifyFileExists:
    def test_file_in_changed_list(self):
        result = verify_criterion(
            "file obscura/arbiter/criteria.py exists",
            files_changed=["obscura/arbiter/criteria.py"],
        )
        assert result.satisfied
        assert result.method == "file_exists"
        assert result.confidence >= 0.85

    def test_file_on_disk(self, tmp_path):
        target = tmp_path / "myfile.py"
        target.write_text("# hello")
        result = verify_criterion(
            f"file {target} exists",
            files_changed=[],
        )
        assert result.satisfied
        assert result.method == "file_exists"

    def test_file_not_found(self):
        result = verify_criterion(
            "file /nonexistent/path/missing.py exists",
            files_changed=[],
            output_text="nothing relevant",
        )
        assert not result.satisfied
        assert result.method == "file_exists"

    def test_file_mentioned_in_output(self):
        result = verify_criterion(
            "file report.txt exists",
            files_changed=[],
            output_text="Created report.txt successfully",
        )
        assert result.satisfied
        assert result.confidence < 0.9  # output-only is lower confidence

    def test_not_file_criterion(self):
        result = verify_criterion("run the migration", output_text="migration done")
        assert result.method != "file_exists"


# ---------------------------------------------------------------------------
# _verify_no_errors
# ---------------------------------------------------------------------------


class TestVerifyNoErrors:
    def test_no_errors_with_zero_count(self):
        result = verify_criterion("no errors", error_count=0, output_text="all good")
        assert result.satisfied
        assert result.method == "no_errors"

    def test_errors_present_via_count(self):
        result = verify_criterion("no errors", error_count=3)
        assert not result.satisfied
        assert result.method == "no_errors"

    def test_error_signals_in_output(self):
        result = verify_criterion(
            "zero errors",
            error_count=0,
            output_text="error: import failed\nexception: traceback\nfailure occurred",
        )
        assert not result.satisfied

    def test_not_no_errors_criterion(self):
        result = verify_criterion("ship the feature", output_text="shipped")
        assert result.method != "no_errors"


# ---------------------------------------------------------------------------
# Keyword fallback
# ---------------------------------------------------------------------------


class TestKeywordFallback:
    def test_good_overlap(self):
        result = verify_criterion(
            "API endpoint tested and documented",
            output_text="tested the API endpoint and wrote documentation",
        )
        assert result.satisfied
        assert result.method == "keyword"
        assert result.confidence <= 0.7  # capped

    def test_low_overlap(self):
        result = verify_criterion(
            "database migrations complete",
            output_text="updated the README",
        )
        assert not result.satisfied
        assert result.method == "keyword"

    def test_empty_criterion(self):
        result = verify_criterion("", output_text="anything")
        # No keywords → treated as satisfied with low confidence
        assert result.satisfied
        assert result.confidence <= 0.6


# ---------------------------------------------------------------------------
# verify_criteria (batch)
# ---------------------------------------------------------------------------


class TestVerifyCriteria:
    def test_all_satisfied(self):
        criteria = ["tests pass", "no errors"]
        score, issues, results = verify_criteria(
            criteria,
            test_outcome=_outcome(passed=10),
            error_count=0,
            output_text="10 passed",
        )
        assert score == 1.0
        assert issues == []
        assert len(results) == 2

    def test_partial_satisfaction(self):
        criteria = ["tests pass", "no errors"]
        score, issues, results = verify_criteria(
            criteria,
            test_outcome=_outcome(passed=0, failed=3),
            error_count=0,
        )
        assert score == 0.5
        # One criterion failed → at least one issue
        assert any("unmet" in i.lower() or "criteria" in i.lower() for i in issues)

    def test_empty_criteria(self):
        score, issues, results = verify_criteria([])
        assert score == 1.0
        assert issues == []
        assert results == []

    def test_all_failed_emits_summary(self):
        criteria = ["tests pass", "lint clean", "no errors"]
        score, issues, results = verify_criteria(
            criteria,
            test_outcome=_outcome(passed=0, failed=5),
            error_count=2,
            output_text="Found 4 errors.",
        )
        assert score < 0.5
        # Should have a summary line about how many criteria met
        assert any("/" in i for i in issues)

    def test_result_count_matches_criteria(self):
        criteria = ["tests pass", "file foo.py exists", "no errors"]
        _, _, results = verify_criteria(
            criteria,
            test_outcome=_outcome(passed=1),
            files_changed=["foo.py"],
            error_count=0,
        )
        assert len(results) == 3
        assert all(isinstance(r, CriterionResult) for r in results)

    def test_stemmed_keywords_match(self):
        """Improved stemmer: 'validators'/'validation' should overlap."""
        result = verify_criterion(
            "validators implemented for all inputs",
            output_text="added validation logic for user inputs and running tests",
        )
        # keyword fallback — should have decent overlap thanks to stemmer
        assert result.method == "keyword"
        assert result.satisfied
