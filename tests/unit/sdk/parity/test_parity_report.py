from __future__ import annotations

from sdk.internal.types import Backend
from sdk.parity.defaults import default_backend_conformance
from sdk.parity.profiles import PROFILES
from sdk.parity.report import to_markdown
from sdk.parity.scoring import score_report_with_conformance


def test_default_backend_conformance_is_complete() -> None:
    conformance = default_backend_conformance()
    assert len(conformance) == 5
    assert {c.backend for c in conformance} == {
        Backend.OPENAI,
        Backend.MOONSHOT,
        Backend.CLAUDE,
        Backend.COPILOT,
        Backend.LOCALLLM,
    }
    assert all(c.percent == 100.0 for c in conformance)


def test_markdown_includes_method_conformance_table() -> None:
    conformance = default_backend_conformance()
    report = score_report_with_conformance(PROFILES, conformance)
    md = to_markdown(report)
    assert "## Method Conformance" in md
    assert "| Backend | Passed | Total | Percent |" in md
    assert "| openai |" in md
    assert "| moonshot |" in md
    assert "| claude |" in md
    assert "| copilot |" in md
    assert "| localllm |" in md
