"""Formatting helpers for parity reports."""

from __future__ import annotations

from obscura.parity.models import ParityReport
from obscura.parity.scoring import parity_percent


def to_markdown(report: ParityReport) -> str:
    """Render a human-readable markdown parity report."""
    lines = [
        "# Parity Matrix",
        "",
        "| Backend | Score | Max | Percent |",
        "|---|---:|---:|---:|",
    ]
    for score in report.backend_scores:
        pct = 0.0 if score.max_score == 0 else (score.score / score.max_score) * 100.0
        lines.append(
            f"| {score.backend.value} | {score.score:.1f} | {score.max_score:.1f} | {pct:.1f}% |"
        )

    lines.extend(
        [
            "",
            f"Overall: **{parity_percent(report):.1f}%**",
        ]
    )

    if report.backend_conformance:
        lines.extend(
            [
                "",
                "## Method Conformance",
                "",
                "| Backend | Passed | Total | Percent |",
                "|---|---:|---:|---:|",
            ]
        )
        for conf in report.backend_conformance:
            lines.append(
                f"| {conf.backend.value} | {conf.passed} | {conf.total} | {conf.percent:.1f}% |"
            )

    if report.residual_risks:
        lines.extend(["", "## Residual Risks"])
        for risk in report.residual_risks:
            lines.append(f"- {risk}")

    return "\n".join(lines)
