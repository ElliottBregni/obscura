"""End-to-end browser tool eval suite.

These tests are the CI-friendly half of the browser-eval story:

* They load eval specs from ``.obscura/evals/browser_tools.toml``.
* They wire a :class:`BrowserToolStubBridge` into a fresh
  :class:`ToolRegistry` via the same proxy-spec helper the production
  client uses.
* They drive a :class:`ScriptedBackend` whose canned tool calls
  exercise each scenario, then run the case through :class:`EvalEngine`
  and verify the deterministic score is 1.0.

No real Chrome, no LLM API, no network — every dependency is in-memory.
The rubric / LLM-as-judge variants live in
``test_browser_rubric_suite.py`` and are gated by an env var.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from obscura.eval.models import CompiledEvalCase

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[4]
SUITE_PATH = REPO_ROOT / "obscura" / "eval" / "builtins" / "browser_tools.toml"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_bridge() -> BrowserToolStubBridge:
    return BrowserToolStubBridge.with_default_page()


@pytest.fixture
async def populated_registry(
    stub_bridge: BrowserToolStubBridge,
) -> ToolRegistry:
    """Tool registry with every stub-bridge tool bound as a proxy spec."""
    registry = ToolRegistry()
    for raw in await stub_bridge.list_tools():
        # ``_build_proxy_spec`` only calls ``client.call(name, kwargs)`` —
        # it never reaches into the client's internals — so the stub
        # bridge satisfies the contract structurally.
        registry.register(_build_proxy_spec(raw, stub_bridge))  # type: ignore[arg-type]
    return registry


@pytest.fixture
def compiled_cases() -> tuple[CompiledEvalCase, ...]:
    if not SUITE_PATH.exists():
        pytest.fail(f"missing eval suite at {SUITE_PATH}")
    return compile_suite_from_path(SUITE_PATH)


def _case_by_id(cases: tuple[CompiledEvalCase, ...], case_id: str) -> CompiledEvalCase:
    for case in cases:
        if case.id == case_id:
            return case
    msg = f"case {case_id!r} not found in suite"
    raise AssertionError(msg)


# ---------------------------------------------------------------------------
# Suite-level smoke tests — make sure the spec parses + discovery works.
# ---------------------------------------------------------------------------


class TestSuiteDiscovery:
    def test_suite_file_exists(self) -> None:
        assert SUITE_PATH.exists(), f"missing {SUITE_PATH}"

    def test_suite_parses(self, compiled_cases: tuple[CompiledEvalCase, ...]) -> None:
        ids = {c.id for c in compiled_cases}
        assert ids == {
            "read-page-contract",
            "click-then-read",
            "fill-then-submit",
            "cheap-vs-cdp-escalation",
        }

    def test_every_case_has_assertions(
        self, compiled_cases: tuple[CompiledEvalCase, ...]
    ) -> None:
        for case in compiled_cases:
            assert case.assertions or case.expect_tool_calls, (
                f"case {case.id} has no assertions"
            )


# ---------------------------------------------------------------------------
# Stub-bridge unit tests — verify the canned page mutates as expected.
# ---------------------------------------------------------------------------


class TestStubBridge:
    async def test_list_tools_surface(self, stub_bridge: BrowserToolStubBridge) -> None:
        tools = await stub_bridge.list_tools()
        names = {t["name"] for t in tools}
        assert {
            "browser_read_page",
            "browser_click",
            "browser_fill",
            "browser_press_key",
            "browser_type_text",
        } <= names

    async def test_read_page_returns_page_state(
        self, stub_bridge: BrowserToolStubBridge
    ) -> None:
        result = await stub_bridge.call("browser_read_page", {})
        assert result["title"] == "Obscura Eval Fixture"
        assert "Welcome" in result["headings"]
        assert len(result["links"]) == 3

    async def test_click_toggles_state(
        self, stub_bridge: BrowserToolStubBridge
    ) -> None:
        page = await stub_bridge.call("browser_read_page", {})
        assert page["buttons"][0]["state"] == "off"

        click_result = await stub_bridge.call("browser_click", {"selector": "#toggle"})
        assert click_result["state"] == "on"

        page2 = await stub_bridge.call("browser_read_page", {})
        button = next(b for b in page2["buttons"] if b["selector"] == "#toggle")
        assert button["state"] == "on"

    async def test_fill_and_press_key_round_trip(
        self, stub_bridge: BrowserToolStubBridge
    ) -> None:
        await stub_bridge.call(
            "browser_fill", {"selector": "#email", "value": "me@example.com"}
        )
        result = await stub_bridge.call(
            "browser_press_key", {"key": "Enter", "selector": "#email"}
        )
        assert result["submitted"] is True
        assert result["value"] == "me@example.com"
        assert stub_bridge.page.fields["#email"].submitted_value == "me@example.com"

    async def test_silent_fill_does_not_mutate_when_flag_set(self) -> None:
        page = FakePage.default()
        page.fill_silently_fails = True
        bridge = BrowserToolStubBridge(page)

        await bridge.call("browser_fill", {"selector": "#search", "value": "obscura"})
        assert page.fields["#search"].value == ""

        # CDP-equivalent always wins.
        await bridge.call(
            "browser_type_text", {"selector": "#search", "text": "obscura"}
        )
        assert page.fields["#search"].value == "obscura"

    async def test_unknown_tool_raises(
        self, stub_bridge: BrowserToolStubBridge
    ) -> None:
        with pytest.raises(RuntimeError, match="unknown stub tool"):
            await stub_bridge.call("browser_does_not_exist", {})


# ---------------------------------------------------------------------------
# Eval-engine roundtrips — drive each case end-to-end.
# ---------------------------------------------------------------------------


class TestEvalEngineRoundtrip:
    async def test_read_page_contract(
        self,
        stub_bridge: BrowserToolStubBridge,
        populated_registry: ToolRegistry,
        compiled_cases: tuple[CompiledEvalCase, ...],
    ) -> None:
        del stub_bridge  # bridge state is exercised through the registry
        case = _case_by_id(compiled_cases, "read-page-contract")
        backend = ScriptedBackend(
            script=[
                ScriptedTurn(tool_calls=[ScriptedToolCall(name="browser_read_page")]),
                ScriptedTurn(text="The page title is Obscura Eval Fixture."),
            ]
        )
        engine = EvalEngine(backend, populated_registry)
        result = await engine.run_case(case, run_id="test-run")
        assert result.verdict == EvalVerdict.PASS, result.assertion_outcomes
        assert result.deterministic_score == 1.0
        assert any(
            tc.tool_name == "browser_read_page" for tc in result.tool_calls_observed
        )

    async def test_click_then_read(
        self,
        stub_bridge: BrowserToolStubBridge,
        populated_registry: ToolRegistry,
        compiled_cases: tuple[CompiledEvalCase, ...],
    ) -> None:
        case = _case_by_id(compiled_cases, "click-then-read")
        backend = ScriptedBackend(
            script=[
                ScriptedTurn(
                    tool_calls=[
                        ScriptedToolCall(
                            name="browser_click", input={"selector": "#toggle"}
                        ),
                    ]
                ),
                ScriptedTurn(tool_calls=[ScriptedToolCall(name="browser_read_page")]),
                ScriptedTurn(text="Toggle is now on."),
            ]
        )
        engine = EvalEngine(backend, populated_registry)
        result = await engine.run_case(case, run_id="test-run")
        assert result.verdict == EvalVerdict.PASS, result.assertion_outcomes
        assert result.deterministic_score == 1.0

        # Verify the FakePage actually mutated through the proxy spec.
        assert stub_bridge.page.buttons["#toggle"].state == "on"
        assert stub_bridge.page.buttons["#toggle"].click_count == 1

    async def test_fill_then_submit(
        self,
        stub_bridge: BrowserToolStubBridge,
        populated_registry: ToolRegistry,
        compiled_cases: tuple[CompiledEvalCase, ...],
    ) -> None:
        case = _case_by_id(compiled_cases, "fill-then-submit")
        backend = ScriptedBackend(
            script=[
                ScriptedTurn(
                    tool_calls=[
                        ScriptedToolCall(
                            name="browser_fill",
                            input={
                                "selector": "#email",
                                "value": "me@example.com",
                            },
                        ),
                    ]
                ),
                ScriptedTurn(
                    tool_calls=[
                        ScriptedToolCall(
                            name="browser_press_key",
                            input={"key": "Enter", "selector": "#email"},
                        ),
                    ]
                ),
                ScriptedTurn(text="Submitted."),
            ]
        )
        engine = EvalEngine(backend, populated_registry)
        result = await engine.run_case(case, run_id="test-run")
        assert result.verdict == EvalVerdict.PASS, result.assertion_outcomes
        assert result.deterministic_score == 1.0

        # Round-trip check: the stub recorded the submission.
        assert stub_bridge.page.fields["#email"].submitted_value == "me@example.com"
        assert stub_bridge.page.submit_log == [
            {"selector": "#email", "value": "me@example.com"},
        ]

    async def test_cheap_vs_cdp_escalation(
        self,
        compiled_cases: tuple[CompiledEvalCase, ...],
    ) -> None:
        # Set up a page where the cheap path silently fails. The model is
        # then expected to (1) call browser_fill, (2) verify with
        # browser_read_page, (3) escalate to browser_type_text. The
        # tool_sequence + arg_exact_match assertions encode that decision.
        page = FakePage.default()
        page.fill_silently_fails = True
        bridge = BrowserToolStubBridge(page)
        registry = ToolRegistry()
        for raw in await bridge.list_tools():
            registry.register(_build_proxy_spec(raw, bridge))  # type: ignore[arg-type]

        case = _case_by_id(compiled_cases, "cheap-vs-cdp-escalation")
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
                ScriptedTurn(text="Escalated to CDP — value now set."),
            ]
        )
        engine = EvalEngine(backend, registry)
        result = await engine.run_case(case, run_id="test-run")
        assert result.verdict == EvalVerdict.PASS, result.assertion_outcomes
        assert result.deterministic_score == 1.0

        # CDP path actually wrote the value despite the silent-fail flag.
        assert page.fields["#search"].value == "obscura"
