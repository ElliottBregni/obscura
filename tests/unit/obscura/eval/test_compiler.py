"""Tests for eval compiler."""

from __future__ import annotations

import tomllib
from pathlib import Path
from tempfile import NamedTemporaryFile

from obscura.eval.compiler import compile_suite, compile_suite_from_path
from obscura.eval.specs import (
    EvalAssertion,
    EvalCaseSpec,
    EvalExpectedToolCall,
    EvalJudgeSpec,
    EvalSuiteMeta,
    EvalSuiteSpec,
)


def _make_suite() -> EvalSuiteSpec:
    return EvalSuiteSpec(
        meta=EvalSuiteMeta(
            id="test-suite", title="Test Suite",
            backend="claude", model="sonnet",
        ),
        cases=[
            EvalCaseSpec(
                id="case-1",
                title="Case 1",
                prompt="Read a file",
                assertions=[
                    EvalAssertion(kind="tool_name_match", expected="read_file"),
                ],
                expect_tool_calls=[
                    EvalExpectedToolCall(name="read_file", args_contain={"path": "/tmp/x"}),
                ],
                judge=EvalJudgeSpec(criteria="Was it correct?", rubric="5=perfect, 1=fail"),
            ),
            EvalCaseSpec(
                id="case-2",
                title="Case 2",
                prompt="Answer a question",
                backend="openai",
                model="gpt-4",
            ),
        ],
    )


class TestCompileSuite:
    def test_compiles_cases(self) -> None:
        suite = _make_suite()
        cases = compile_suite(suite)
        assert len(cases) == 2
        assert cases[0].id == "case-1"
        assert cases[1].id == "case-2"

    def test_suite_defaults_propagate(self) -> None:
        suite = _make_suite()
        cases = compile_suite(suite)
        # Case 1 inherits suite backend/model
        assert cases[0].backend == "claude"
        assert cases[0].model == "sonnet"

    def test_case_override_takes_precedence(self) -> None:
        suite = _make_suite()
        cases = compile_suite(suite)
        # Case 2 overrides backend/model
        assert cases[1].backend == "openai"
        assert cases[1].model == "gpt-4"

    def test_assertions_compiled(self) -> None:
        suite = _make_suite()
        cases = compile_suite(suite)
        assert len(cases[0].assertions) == 1
        assert cases[0].assertions[0].kind == "tool_name_match"
        assert cases[0].assertions[0].expected == ("read_file",)

    def test_expected_tool_calls_compiled(self) -> None:
        suite = _make_suite()
        cases = compile_suite(suite)
        assert len(cases[0].expect_tool_calls) == 1
        etc = cases[0].expect_tool_calls[0]
        assert etc.name == "read_file"
        assert etc.args_contain == (("path", "/tmp/x"),)

    def test_judge_compiled(self) -> None:
        suite = _make_suite()
        cases = compile_suite(suite)
        assert cases[0].judge_criteria == "Was it correct?"
        assert cases[0].judge_rubric == "5=perfect, 1=fail"

    def test_no_judge(self) -> None:
        suite = _make_suite()
        cases = compile_suite(suite)
        assert cases[1].judge_criteria == ""

    def test_suite_id_set(self) -> None:
        suite = _make_suite()
        cases = compile_suite(suite)
        assert all(c.suite_id == "test-suite" for c in cases)

    def test_tags_are_tuples(self) -> None:
        suite = EvalSuiteSpec(
            meta=EvalSuiteMeta(id="s", title="T"),
            cases=[
                EvalCaseSpec(
                    id="c", title="C", prompt="P",
                    tags=["fast", "tools"],
                ),
            ],
        )
        cases = compile_suite(suite)
        assert cases[0].tags == ("fast", "tools")


class TestCompileSuiteFromPath:
    def test_loads_and_compiles(self, tmp_path: Path) -> None:
        toml_content = b"""
[meta]
id = "file-suite"
title = "From File"
backend = "claude"
model = "sonnet"

[[cases]]
id = "c1"
title = "Case 1"
prompt = "Hello"

  [[cases.assertions]]
  kind = "output_contains"
  substring = "hi"
"""
        toml_file = tmp_path / "test.toml"
        toml_file.write_bytes(toml_content)

        cases = compile_suite_from_path(toml_file)
        assert len(cases) == 1
        assert cases[0].id == "c1"
        assert cases[0].backend == "claude"
        assert cases[0].assertions[0].kind == "output_contains"
        assert cases[0].assertions[0].substring == "hi"
