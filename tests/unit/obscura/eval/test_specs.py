"""Tests for eval Pydantic spec models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from obscura.eval.specs import (
    EvalAssertion,
    EvalCaseSpec,
    EvalExpectedToolCall,
    EvalJudgeSpec,
    EvalRegressionSpec,
    EvalSuiteMeta,
    EvalSuiteSpec,
)


class TestEvalSuiteMeta:
    def test_minimal(self) -> None:
        meta = EvalSuiteMeta(id="suite-1", title="Test Suite")
        assert meta.id == "suite-1"
        assert meta.version == "1"
        assert meta.tags == []
        assert meta.backend is None

    def test_full(self) -> None:
        meta = EvalSuiteMeta(
            id="suite-2",
            title="Full",
            version="2",
            tags=["a", "b"],
            backend="claude",
            model="claude-sonnet-4-5-20250929",
        )
        assert meta.backend == "claude"
        assert meta.model == "claude-sonnet-4-5-20250929"

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EvalSuiteMeta(id="x", title="y", unknown_field="z")


class TestEvalAssertion:
    def test_tool_name_match(self) -> None:
        a = EvalAssertion(kind="tool_name_match", expected="read_file", turn=1)
        assert a.kind == "tool_name_match"
        assert a.expected == "read_file"
        assert a.turn == 1

    def test_output_contains(self) -> None:
        a = EvalAssertion(kind="output_contains", substring="hello")
        assert a.substring == "hello"

    def test_tool_sequence(self) -> None:
        a = EvalAssertion(kind="tool_sequence", expected=["read", "write"])
        assert a.expected == ["read", "write"]


class TestEvalExpectedToolCall:
    def test_minimal(self) -> None:
        tc = EvalExpectedToolCall(name="bash")
        assert tc.name == "bash"
        assert tc.order is None
        assert tc.args_contain == {}

    def test_with_args(self) -> None:
        tc = EvalExpectedToolCall(
            name="read_file",
            order=1,
            args_contain={"path": "/tmp/x"},
        )
        assert tc.args_contain["path"] == "/tmp/x"


class TestEvalJudgeSpec:
    def test_defaults(self) -> None:
        j = EvalJudgeSpec(criteria="Was it correct?")
        assert j.pass_threshold == 3.0
        assert j.rubric == ""


class TestEvalRegressionSpec:
    def test_defaults(self) -> None:
        r = EvalRegressionSpec()
        assert r.score_threshold == 0.80
        assert r.max_score_delta == -0.10


class TestEvalCaseSpec:
    def test_minimal(self) -> None:
        c = EvalCaseSpec(id="case-1", title="Test", prompt="Do something")
        assert c.max_turns == 10
        assert c.tool_mode == "live"
        assert c.assertions == []

    def test_full(self) -> None:
        c = EvalCaseSpec(
            id="case-2",
            title="Full Case",
            prompt="Read /tmp/foo",
            max_turns=5,
            backend="openai",
            model="gpt-4",
            tags=["fast"],
            assertions=[EvalAssertion(kind="output_contains", substring="bar")],
            expect_tool_calls=[EvalExpectedToolCall(name="read_file")],
            judge=EvalJudgeSpec(criteria="Quality?"),
            regression=EvalRegressionSpec(score_threshold=0.9),
        )
        assert len(c.assertions) == 1
        assert c.judge is not None
        assert c.regression is not None


class TestEvalSuiteSpec:
    def test_roundtrip(self) -> None:
        suite = EvalSuiteSpec(
            meta=EvalSuiteMeta(id="s1", title="Suite"),
            cases=[
                EvalCaseSpec(id="c1", title="Case 1", prompt="Hello"),
            ],
        )
        assert len(suite.cases) == 1
        assert suite.meta.id == "s1"

    def test_extra_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EvalSuiteSpec(
                meta=EvalSuiteMeta(id="s1", title="Suite"),
                cases=[],
                extra_field="bad",
            )
