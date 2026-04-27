"""LLM-as-judge rubric variants of the browser eval suite.

These cases require a real judge backend (network + API key), so they
are gated behind the ``OBSCURA_EVAL_RUBRIC`` env var. Without the var
set, the tests are skipped — keeps CI green and cheap.

Set ``OBSCURA_EVAL_RUBRIC=1`` plus a working ``ANTHROPIC_API_KEY`` to
run them locally.

TODO: wire an actual judge backend here once we wire AnthropicEvalBackend
to honour the rubric block end-to-end. For now this file exercises the
deterministic assertions in the rubric suite (they still pass without a
judge — the judge score is just optional) and skips the judge-only cases.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from obscura.core.tools import ToolRegistry
from obscura.eval.browser_stub import BrowserToolStubBridge, FakePage
from obscura.eval.compiler import compile_suite_from_path
from obscura.eval.engine import EvalEngine
from obscura.eval.models import EvalVerdict
from obscura.eval.scripted_backend import (
    ScriptedBackend,
    ScriptedToolCall,
    ScriptedTurn,
)
from obscura.integrations.browser.client import _build_proxy_spec

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[4]
SUITE_PATH = REPO_ROOT / "obscura" / "eval" / "builtins" / "browser_tools_rubric.toml"


def _rubric_enabled() -> bool:
    return os.environ.get("OBSCURA_EVAL_RUBRIC") == "1"


class TestRubricSuiteShape:
    """Suite-shape sanity checks — always run, no judge needed."""

    def test_suite_file_exists(self) -> None:
        assert SUITE_PATH.exists(), f"missing {SUITE_PATH}"

    def test_suite_parses_with_judge_block(self) -> None:
        cases = compile_suite_from_path(SUITE_PATH)
        ids = {c.id for c in cases}
        assert "rubric-cheap-vs-cdp-reasoning" in ids
        case = next(c for c in cases if c.id == "rubric-cheap-vs-cdp-reasoning")
        assert case.judge_criteria
        assert case.judge_pass_threshold == 3.0


class TestRubricRoundtrip:
    """Full rubric run — gated on OBSCURA_EVAL_RUBRIC=1."""

    @pytest.mark.skipif(
        not _rubric_enabled(),
        reason=(
            "Rubric eval requires a judge backend + API key. "
            "Set OBSCURA_EVAL_RUBRIC=1 to run locally."
        ),
    )
    async def test_rubric_cheap_vs_cdp_reasoning(self) -> None:
        # Deterministic part: the assertions still apply with or without
        # a judge. We don't pass judge_backend here because the test
        # body's purpose is to verify the deterministic score reaches
        # 1.0 and the rubric block round-trips through compile/score.
        # When the user opts in via OBSCURA_EVAL_RUBRIC=1 AND wires a
        # judge backend, swap the engine call below to pass it through.
        page = FakePage.default()
        page.fill_silently_fails = True
        bridge = BrowserToolStubBridge(page)
        registry = ToolRegistry()
        for raw in await bridge.list_tools():
            registry.register(_build_proxy_spec(raw, bridge))  # type: ignore[arg-type]

        cases = compile_suite_from_path(SUITE_PATH)
        case = next(c for c in cases if c.id == "rubric-cheap-vs-cdp-reasoning")

        backend = ScriptedBackend(
            script=[
                ScriptedTurn(
                    tool_calls=[
                        ScriptedToolCall(
                            name="browser_fill",
                            input={"selector": "#search", "value": "obscura"},
                        ),
                    ]
                ),
                ScriptedTurn(tool_calls=[ScriptedToolCall(name="browser_read_page")]),
                ScriptedTurn(
                    tool_calls=[
                        ScriptedToolCall(
                            name="browser_type_text",
                            input={"selector": "#search", "text": "obscura"},
                        ),
                    ]
                ),
                ScriptedTurn(
                    text=(
                        "browser_fill returned a success envelope but the "
                        "value didn't stick — that's the classic isTrusted="
                        "false silent revert. Escalating to browser_type_text "
                        "(CDP) so the input event is real."
                    )
                ),
            ]
        )
        # judge_backend left None on purpose: the deterministic
        # assertions must still pass even without the rubric grader.
        engine = EvalEngine(backend, registry)
        result = await engine.run_case(case, run_id="rubric-test-run")
        assert result.verdict == EvalVerdict.PASS, result.assertion_outcomes
        assert result.deterministic_score == 1.0
