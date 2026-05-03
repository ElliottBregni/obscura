from __future__ import annotations

from typing import Any, cast


def score_sync_report(report: dict[str, Any]) -> dict[str, Any]:
    """Produce a lightweight score and decision for a VaultSync report.

    Heuristic: fewer changed files -> higher score. Score in [0.0, 1.0].
    Decision is 'accept' for score >= 0.8 else 'review'.
    """
    added = len(report.get("added", []) or [])
    modified = len(report.get("modified", []) or [])
    removed = len(report.get("removed", []) or [])
    total = added + modified + removed

    if total == 0:
        score = 1.0
    else:
        score = max(0.0, 1.0 - (total / 100.0))

    decision = "accept" if score >= 0.8 else "review"
    return {"score": score, "decision": decision, "counts": {"added": added, "modified": modified, "removed": removed}}


def auto_accept(report: dict[str, Any], threshold: float = 0.8) -> bool:
    return cast(float, score_sync_report(report)["score"]) >= float(threshold)
