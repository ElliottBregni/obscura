"""SQLite persistence for eval results, baselines, and run history."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from obscura.core.paths import resolve_obscura_evals_dir
from obscura.eval.models import (
    AssertionOutcome,
    AssertionResult,
    EvalCaseResult,
    EvalRunSummary,
    EvalVerdict,
    JudgeScore,
    ToolCallRecord,
)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS eval_runs (
    run_id       TEXT PRIMARY KEY,
    suite_id     TEXT NOT NULL,
    backend      TEXT NOT NULL,
    model        TEXT NOT NULL,
    total_cases  INTEGER NOT NULL DEFAULT 0,
    passed       INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    regressions  INTEGER NOT NULL DEFAULT 0,
    errors       INTEGER NOT NULL DEFAULT 0,
    avg_deterministic_score REAL NOT NULL DEFAULT 0.0,
    avg_judge_score         REAL,
    avg_composite_score     REAL NOT NULL DEFAULT 0.0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_case_results (
    run_id              TEXT NOT NULL,
    case_id             TEXT NOT NULL,
    suite_id            TEXT NOT NULL,
    verdict             TEXT NOT NULL,
    deterministic_score REAL NOT NULL DEFAULT 0.0,
    judge_score         REAL,
    composite_score     REAL NOT NULL DEFAULT 0.0,
    output_text         TEXT NOT NULL DEFAULT '',
    tool_calls_json     TEXT NOT NULL DEFAULT '[]',
    assertion_outcomes_json TEXT NOT NULL DEFAULT '[]',
    judge_detail_json   TEXT,
    turns_used          INTEGER NOT NULL DEFAULT 0,
    latency_ms          INTEGER NOT NULL DEFAULT 0,
    error               TEXT NOT NULL DEFAULT '',
    events_json         TEXT NOT NULL DEFAULT '[]',
    created_at          TEXT NOT NULL,
    PRIMARY KEY (run_id, case_id)
);

CREATE TABLE IF NOT EXISTS eval_baselines (
    case_id    TEXT NOT NULL,
    suite_id   TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    score      REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (case_id, suite_id)
);
"""


class EvalResultStore:
    """Thread-safe SQLite store for eval results."""

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            evals_dir = resolve_obscura_evals_dir()
            evals_dir.mkdir(parents=True, exist_ok=True)
            db_path = evals_dir / "results.db"
        self._db_path = db_path
        self._local = threading.local()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        conn = self._conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    # ------------------------------------------------------------------
    # Sync methods (called via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _save_run_sync(self, summary: EvalRunSummary) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO eval_runs "
            "(run_id, suite_id, backend, model, total_cases, passed, failed, "
            "regressions, errors, avg_deterministic_score, avg_judge_score, "
            "avg_composite_score, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                summary.run_id,
                summary.suite_id,
                summary.backend,
                summary.model,
                summary.total_cases,
                summary.passed,
                summary.failed,
                summary.regressions,
                summary.errors,
                summary.avg_deterministic_score,
                summary.avg_judge_score,
                summary.avg_composite_score,
                summary.timestamp.isoformat(),
            ),
        )
        for cr in summary.case_results:
            self._save_case_result_sync(cr)
        conn.commit()

    def _save_case_result_sync(self, result: EvalCaseResult) -> None:
        conn = self._conn()

        tool_calls_json = json.dumps(
            [
                {
                    "turn": tc.turn,
                    "tool_name": tc.tool_name,
                    "tool_input": tc.tool_input,
                    "tool_result": tc.tool_result,
                    "is_error": tc.is_error,
                    "latency_ms": tc.latency_ms,
                }
                for tc in result.tool_calls_observed
            ]
        )

        assertion_outcomes_json = json.dumps(
            [
                {
                    "assertion_kind": ao.assertion_kind,
                    "result": ao.result.value,
                    "message": ao.message,
                }
                for ao in result.assertion_outcomes
            ]
        )

        judge_detail_json = None
        if result.judge_detail is not None:
            judge_detail_json = json.dumps(
                {
                    "score": result.judge_detail.score,
                    "reasoning": result.judge_detail.reasoning,
                    "criteria": result.judge_detail.criteria,
                }
            )

        events_json = json.dumps(list(result.events))

        conn.execute(
            "INSERT OR REPLACE INTO eval_case_results "
            "(run_id, case_id, suite_id, verdict, deterministic_score, "
            "judge_score, composite_score, output_text, tool_calls_json, "
            "assertion_outcomes_json, judge_detail_json, turns_used, "
            "latency_ms, error, events_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.run_id,
                result.case_id,
                result.suite_id,
                result.verdict.value,
                result.deterministic_score,
                result.judge_score,
                result.composite_score,
                result.output_text,
                tool_calls_json,
                assertion_outcomes_json,
                judge_detail_json,
                result.turns_used,
                result.latency_ms,
                result.error,
                events_json,
                result.timestamp.isoformat(),
            ),
        )

    def _get_case_result_sync(
        self, run_id: str, case_id: str,
    ) -> EvalCaseResult | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM eval_case_results WHERE run_id = ? AND case_id = ?",
            (run_id, case_id),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_case_result(row)

    def _get_baseline_sync(
        self, case_id: str, suite_id: str,
    ) -> tuple[str, float] | None:
        """Return ``(run_id, score)`` for the baseline or ``None``."""
        conn = self._conn()
        row = conn.execute(
            "SELECT run_id, score FROM eval_baselines "
            "WHERE case_id = ? AND suite_id = ?",
            (case_id, suite_id),
        ).fetchone()
        if row is None:
            return None
        return (row["run_id"], row["score"])

    def _promote_baseline_sync(
        self, run_id: str, suite_id: str,
    ) -> None:
        conn = self._conn()
        rows = conn.execute(
            "SELECT case_id, composite_score FROM eval_case_results "
            "WHERE run_id = ? AND suite_id = ?",
            (run_id, suite_id),
        ).fetchall()
        now = datetime.now(UTC).isoformat()
        for row in rows:
            conn.execute(
                "INSERT OR REPLACE INTO eval_baselines "
                "(case_id, suite_id, run_id, score, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (row["case_id"], suite_id, run_id, row["composite_score"], now),
            )
        conn.commit()

    def _list_runs_sync(
        self, suite_id: str | None = None, limit: int = 20,
    ) -> list[dict[str, object]]:
        conn = self._conn()
        if suite_id:
            rows = conn.execute(
                "SELECT * FROM eval_runs WHERE suite_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (suite_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM eval_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _list_baselines_sync(
        self, suite_id: str | None = None,
    ) -> list[dict[str, object]]:
        conn = self._conn()
        if suite_id:
            rows = conn.execute(
                "SELECT * FROM eval_baselines WHERE suite_id = ? "
                "ORDER BY case_id",
                (suite_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM eval_baselines ORDER BY suite_id, case_id",
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Row deserialization
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_case_result(row: sqlite3.Row) -> EvalCaseResult:
        tool_calls_raw = json.loads(row["tool_calls_json"])
        tool_calls = tuple(
            ToolCallRecord(
                turn=tc["turn"],
                tool_name=tc["tool_name"],
                tool_input=tc.get("tool_input", {}),
                tool_result=tc.get("tool_result", ""),
                is_error=tc.get("is_error", False),
                latency_ms=tc.get("latency_ms", 0),
            )
            for tc in tool_calls_raw
        )

        outcomes_raw = json.loads(row["assertion_outcomes_json"])
        assertion_outcomes = tuple(
            AssertionOutcome(
                assertion_kind=ao["assertion_kind"],
                result=AssertionResult(ao["result"]),
                message=ao.get("message", ""),
            )
            for ao in outcomes_raw
        )

        judge_detail = None
        if row["judge_detail_json"]:
            jd = json.loads(row["judge_detail_json"])
            judge_detail = JudgeScore(
                score=jd["score"],
                reasoning=jd["reasoning"],
                criteria=jd["criteria"],
            )

        events = tuple(json.loads(row["events_json"]))

        return EvalCaseResult(
            case_id=row["case_id"],
            suite_id=row["suite_id"],
            run_id=row["run_id"],
            verdict=EvalVerdict(row["verdict"]),
            deterministic_score=row["deterministic_score"],
            judge_score=row["judge_score"],
            composite_score=row["composite_score"],
            assertion_outcomes=assertion_outcomes,
            judge_detail=judge_detail,
            tool_calls_observed=tool_calls,
            output_text=row["output_text"],
            turns_used=row["turns_used"],
            latency_ms=row["latency_ms"],
            error=row["error"],
            events=events,
            timestamp=datetime.fromisoformat(row["created_at"]),
        )

    # ------------------------------------------------------------------
    # Async wrappers
    # ------------------------------------------------------------------

    async def save_run(self, summary: EvalRunSummary) -> None:
        """Persist an eval run and all its case results."""
        await asyncio.to_thread(self._save_run_sync, summary)

    async def get_case_result(
        self, run_id: str, case_id: str,
    ) -> EvalCaseResult | None:
        """Retrieve a single case result."""
        return await asyncio.to_thread(
            self._get_case_result_sync, run_id, case_id,
        )

    async def get_baseline(
        self, case_id: str, suite_id: str,
    ) -> tuple[str, float] | None:
        """Get the baseline ``(run_id, score)`` for a case."""
        return await asyncio.to_thread(
            self._get_baseline_sync, case_id, suite_id,
        )

    async def promote_baseline(self, run_id: str, suite_id: str) -> None:
        """Promote all case results from a run as the new baseline."""
        await asyncio.to_thread(
            self._promote_baseline_sync, run_id, suite_id,
        )

    async def list_runs(
        self, suite_id: str | None = None, limit: int = 20,
    ) -> list[dict[str, object]]:
        """List recent eval runs."""
        return await asyncio.to_thread(
            self._list_runs_sync, suite_id, limit,
        )

    async def list_baselines(
        self, suite_id: str | None = None,
    ) -> list[dict[str, object]]:
        """List current baselines."""
        return await asyncio.to_thread(
            self._list_baselines_sync, suite_id,
        )
