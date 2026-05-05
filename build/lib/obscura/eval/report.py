"""Render eval results as terminal tables, JSON, or markdown."""

from __future__ import annotations

import json
from io import StringIO
from typing import Any

from rich.console import Console
from rich.table import Table

from obscura.eval.models import EvalCaseResult, EvalRunSummary, EvalVerdict

# ---------------------------------------------------------------------------
# Terminal table (rich)
# ---------------------------------------------------------------------------

_VERDICT_STYLE: dict[EvalVerdict, str] = {
    EvalVerdict.PASS: "bold green",
    EvalVerdict.FAIL: "bold red",
    EvalVerdict.REGRESSION: "bold yellow",
    EvalVerdict.ERROR: "bold magenta",
}


def render_table(summary: EvalRunSummary, *, console: Console | None = None) -> str:
    """Render an eval run as a rich terminal table.

    Returns the rendered string (also prints if *console* is given).
    """
    table = Table(
        title=f"Eval Run: {summary.run_id}",
        caption=(
            f"{summary.suite_id} | {summary.backend}/{summary.model} | "
            f"{summary.passed}/{summary.total_cases} passed"
        ),
    )

    table.add_column("Case", style="cyan", no_wrap=True)
    table.add_column("Verdict", justify="center")
    table.add_column("Det Score", justify="right")
    table.add_column("Judge", justify="right")
    table.add_column("Composite", justify="right")
    table.add_column("Turns", justify="right")
    table.add_column("Time (ms)", justify="right")

    for cr in summary.case_results:
        style = _VERDICT_STYLE.get(cr.verdict, "")
        table.add_row(
            cr.case_id,
            f"[{style}]{cr.verdict.value.upper()}[/{style}]",
            f"{cr.deterministic_score:.2f}",
            f"{cr.judge_score:.1f}" if cr.judge_score is not None else "-",
            f"{cr.composite_score:.2f}",
            str(cr.turns_used),
            str(cr.latency_ms),
        )

    # Summary row
    table.add_section()
    table.add_row(
        "TOTAL",
        f"{summary.passed}P/{summary.failed}F/{summary.regressions}R/{summary.errors}E",
        f"{summary.avg_deterministic_score:.2f}",
        f"{summary.avg_judge_score:.1f}"
        if summary.avg_judge_score is not None
        else "-",
        f"{summary.avg_composite_score:.2f}",
        "",
        "",
    )

    buf = StringIO()
    render_console = Console(file=buf, force_terminal=True)
    render_console.print(table)

    if console is not None:
        console.print(table)

    return buf.getvalue()


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _case_result_to_dict(cr: EvalCaseResult) -> dict[str, Any]:
    return {
        "case_id": cr.case_id,
        "verdict": cr.verdict.value,
        "deterministic_score": cr.deterministic_score,
        "judge_score": cr.judge_score,
        "composite_score": cr.composite_score,
        "turns_used": cr.turns_used,
        "latency_ms": cr.latency_ms,
        "error": cr.error or None,
        "tool_calls": [
            {
                "turn": tc.turn,
                "tool_name": tc.tool_name,
                "is_error": tc.is_error,
            }
            for tc in cr.tool_calls_observed
        ],
        "assertions": [
            {
                "kind": ao.assertion_kind,
                "result": ao.result.value,
                "message": ao.message,
            }
            for ao in cr.assertion_outcomes
        ],
    }


def render_json(summary: EvalRunSummary) -> str:
    """Render an eval run as a JSON string."""
    data: dict[str, Any] = {
        "run_id": summary.run_id,
        "suite_id": summary.suite_id,
        "backend": summary.backend,
        "model": summary.model,
        "total_cases": summary.total_cases,
        "passed": summary.passed,
        "failed": summary.failed,
        "regressions": summary.regressions,
        "errors": summary.errors,
        "avg_deterministic_score": summary.avg_deterministic_score,
        "avg_judge_score": summary.avg_judge_score,
        "avg_composite_score": summary.avg_composite_score,
        "timestamp": summary.timestamp.isoformat(),
        "cases": [_case_result_to_dict(cr) for cr in summary.case_results],
    }
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def render_markdown(summary: EvalRunSummary) -> str:
    """Render an eval run as a markdown report."""
    lines: list[str] = [
        f"# Eval Run: {summary.run_id}",
        "",
        f"**Suite:** {summary.suite_id}  ",
        f"**Backend:** {summary.backend} / {summary.model}  ",
        f"**Results:** {summary.passed}/{summary.total_cases} passed, "
        f"{summary.failed} failed, {summary.regressions} regressions, "
        f"{summary.errors} errors  ",
        f"**Avg Composite Score:** {summary.avg_composite_score:.2f}  ",
        "",
        "| Case | Verdict | Det | Judge | Composite | Turns | Time (ms) |",
        "|------|---------|-----|-------|-----------|-------|-----------|",
    ]

    for cr in summary.case_results:
        judge_str = f"{cr.judge_score:.1f}" if cr.judge_score is not None else "-"
        lines.append(
            f"| {cr.case_id} | {cr.verdict.value.upper()} | "
            f"{cr.deterministic_score:.2f} | {judge_str} | "
            f"{cr.composite_score:.2f} | {cr.turns_used} | {cr.latency_ms} |",
        )

    lines.append("")
    return "\n".join(lines)
