"""Pydantic input models for TOML-based eval specifications."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class EvalJudgeSpec(BaseModel):
    """LLM-as-judge configuration for an eval case."""

    model_config = {"extra": "forbid"}

    criteria: str
    rubric: str = ""
    pass_threshold: float = 3.0


class EvalExpectedToolCall(BaseModel):
    """Expected tool call in an eval case."""

    model_config = {"extra": "forbid"}

    name: str
    order: int | None = None
    args_contain: dict[str, Any] = {}


class EvalAssertion(BaseModel):
    """Deterministic assertion for an eval case."""

    model_config = {"extra": "forbid"}

    kind: str  # tool_name_match | output_contains | tool_sequence | no_tool_calls | event_present | arg_exact_match
    turn: int | None = None
    expected: str | list[str] = ""
    substring: str = ""


class EvalRegressionSpec(BaseModel):
    """Regression detection configuration for an eval case."""

    model_config = {"extra": "forbid"}

    baseline_run_id: str = ""
    score_threshold: float = 0.80
    max_score_delta: float = -0.10


class EvalCaseSpec(BaseModel):
    """One eval case within a suite."""

    model_config = {"extra": "forbid"}

    id: str
    title: str
    prompt: str
    max_turns: int = 10
    backend: str | None = None
    model: str | None = None
    tool_mode: str = "live"  # "live" | "record" | "replay"
    fixtures_dir: str = ""
    golden_session_id: str = ""
    tags: list[str] = []
    expect_tool_calls: list[EvalExpectedToolCall] = []
    assertions: list[EvalAssertion] = []
    judge: EvalJudgeSpec | None = None
    regression: EvalRegressionSpec | None = None


class EvalSuiteMeta(BaseModel):
    """Suite-level metadata."""

    model_config = {"extra": "forbid"}

    id: str
    title: str
    version: str = "1"
    tags: list[str] = []
    backend: str | None = None
    model: str | None = None


class EvalSuiteSpec(BaseModel):
    """Top-level eval suite parsed from a TOML file."""

    model_config = {"extra": "forbid"}

    meta: EvalSuiteMeta
    cases: list[EvalCaseSpec]
