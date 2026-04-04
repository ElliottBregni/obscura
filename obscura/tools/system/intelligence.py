"""obscura.tools.system.intelligence — Glass-box observability tools.

Three high-leverage tools that give an agent full introspective access to its
own supervisor state, causal event history, and policy constraints:

  context_snapshot  — Portable serialized bundle of entire agent context
  causal_trace      — Backwards walk through the event log to explain outcomes
  policy_probe      — Pre-flight policy check before tool invocations
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from obscura.core.paths import resolve_obscura_home
from obscura.core.tools import tool
from obscura.tools.policy import ToolPolicy, evaluate_policy
from obscura.tools.policy.engine import _FS_TOOLS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_supervisor_db() -> Path:
    """Return path to the supervisor SQLite database."""
    return resolve_obscura_home() / "supervisor.db"


def _open_db(db_path: Path) -> sqlite3.Connection | None:
    """Open DB read-only; return None if it doesn't exist."""
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(zip(row.keys(), tuple(row), strict=False))


def _rows(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    try:
        cur = conn.execute(sql, params)
        return [_row_to_dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError:
        return []


# ---------------------------------------------------------------------------
# context_snapshot
# ---------------------------------------------------------------------------


@tool(
    "context_snapshot",
    description=(
        "Capture a portable, serialized snapshot of the entire agent context for "
        "the current (or specified) session. Returns a JSON bundle containing the "
        "active run, frozen tool list, recent memory, prompt sections, active policy, "
        "recent heartbeats, and registered hooks. Use this to checkpoint state, debug "
        "unexpected behaviour, or hand off context to another agent."
    ),
    parameters={
        "session_id": {
            "type": "string",
            "description": "Session ID to snapshot. Defaults to the most-recent session.",
        },
        "run_id": {
            "type": "string",
            "description": "Specific run ID within the session. Defaults to the latest run.",
        },
        "include": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Subset of sections to include. Valid values: 'run', 'tools', 'memory', "
                "'prompt', 'policy', 'heartbeats', 'hooks'. Omit to include all."
            ),
        },
    },
)
async def context_snapshot(
    session_id: str = "",
    run_id: str = "",
    include: list[str] | None = None,
) -> str:
    """Return a JSON snapshot of agent context from the supervisor DB."""
    ALL_SECTIONS = {"run", "tools", "memory", "prompt", "policy", "heartbeats", "hooks"}
    sections = set(include) if include else ALL_SECTIONS

    db_path = _get_supervisor_db()
    conn = _open_db(db_path)

    if conn is None:
        return json.dumps(
            {
                "status": "no_db",
                "message": (
                    f"Supervisor database not found at {db_path}. "
                    "The supervisor has not run yet in this environment."
                ),
            },
        )

    try:
        snapshot: dict[str, Any] = {"status": "ok", "db_path": str(db_path)}

        # ── Resolve session_id / run_id ──────────────────────────────────────
        if not session_id:
            rows = _rows(
                conn,
                "SELECT session_id FROM supervisor_runs ORDER BY started_at DESC LIMIT 1",
            )
            session_id = rows[0]["session_id"] if rows else ""

        if not run_id:
            if session_id:
                rows = _rows(
                    conn,
                    "SELECT run_id FROM supervisor_runs WHERE session_id = ? "
                    "ORDER BY started_at DESC LIMIT 1",
                    (session_id,),
                )
            else:
                rows = _rows(
                    conn,
                    "SELECT run_id FROM supervisor_runs ORDER BY started_at DESC LIMIT 1",
                )
            run_id = rows[0]["run_id"] if rows else ""

        snapshot["session_id"] = session_id
        snapshot["run_id"] = run_id

        # ── run ─────────────────────────────────────────────────────────────
        if "run" in sections and run_id:
            runs = _rows(
                conn,
                "SELECT * FROM supervisor_runs WHERE run_id = ?",
                (run_id,),
            )
            snapshot["run"] = runs[0] if runs else {}

        # ── tools ────────────────────────────────────────────────────────────
        if "tools" in sections:
            snapshot["tools"] = (
                _rows(
                    conn,
                    "SELECT tool_name, tool_hash, registered_at FROM tool_registrations "
                    "WHERE run_id = ? ORDER BY tool_name",
                    (run_id,),
                )
                if run_id
                else []
            )

        # ── memory ───────────────────────────────────────────────────────────
        if "memory" in sections:
            snapshot["memory"] = _rows(
                conn,
                "SELECT key, content, importance, recency, relevance, pinned, source_run_id "
                "FROM memory_items ORDER BY importance DESC, recency DESC LIMIT 50",
            )
            snapshot["recent_commits"] = _rows(
                conn,
                "SELECT run_id, committed, deduplicated, gated, errors, committed_at "
                "FROM memory_commits ORDER BY committed_at DESC LIMIT 10",
            )

        # ── prompt ───────────────────────────────────────────────────────────
        if "prompt" in sections and run_id:
            ps = _rows(
                conn,
                "SELECT * FROM prompt_snapshots WHERE run_id = ? "
                "ORDER BY assembled_at DESC LIMIT 1",
                (run_id,),
            )
            snapshot["prompt"] = ps[0] if ps else {}

        # ── policy ───────────────────────────────────────────────────────────
        if "policy" in sections and session_id:
            pv = _rows(
                conn,
                "SELECT * FROM policy_versions WHERE session_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            )
            if pv:
                row = pv[0]
                try:
                    policy_data = json.loads(row.get("policy_json", "{}"))
                except (json.JSONDecodeError, TypeError):
                    policy_data = {}
                snapshot["policy"] = {
                    "version_id": row.get("version_id"),
                    "created_at": row.get("created_at"),
                    "policy": policy_data,
                }
            else:
                snapshot["policy"] = {}

        # ── heartbeats ───────────────────────────────────────────────────────
        if "heartbeats" in sections and session_id:
            snapshot["heartbeats"] = _rows(
                conn,
                "SELECT seq, state, turn_number, elapsed_ms, timestamp "
                "FROM session_heartbeats WHERE session_id = ? "
                "ORDER BY seq DESC LIMIT 20",
                (session_id,),
            )

        # ── hooks ────────────────────────────────────────────────────────────
        if "hooks" in sections and session_id:
            snapshot["hooks"] = _rows(
                conn,
                "SELECT hook_id, hook_point, handler_name, enabled, registered_at "
                "FROM session_hooks WHERE session_id = ? "
                "ORDER BY hook_point, registered_at",
                (session_id,),
            )

    finally:
        conn.close()

    return json.dumps(snapshot, default=str, indent=2)


# ---------------------------------------------------------------------------
# causal_trace
# ---------------------------------------------------------------------------

_CAUSAL_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "tool_execution_start",
        "tool_execution_end",
        "model_turn_start",
        "model_turn_end",
        "memory_commit",
        "memory_gated",
        "state_transition",
        "drift_detected",
        "run_started",
        "run_completed",
        "run_failed",
        "context_built",
        "tools_frozen",
        "prompt_assembled",
        "memory_retrieved",
        "hook_fired",
    },
)


@tool(
    "causal_trace",
    description=(
        "Walk backwards through the supervisor event log to reconstruct the causal "
        "chain leading to a given outcome (e.g. a tool failure, drift detection, or "
        "unexpected memory gate). Returns an ordered list of events with fork-point "
        "identification and a statistical summary. Use this to explain 'why did X "
        "happen?' in the current or a specified session."
    ),
    parameters={
        "session_id": {
            "type": "string",
            "description": "Session ID to trace. Defaults to the most-recent session.",
        },
        "run_id": {
            "type": "string",
            "description": "Specific run ID to trace. Defaults to the latest run.",
        },
        "outcome": {
            "type": "string",
            "description": (
                "Keyword or event kind to search for as the terminal outcome "
                "(e.g. 'drift_detected', 'run_failed', 'memory_gated', or any "
                "tool name). The trace walks backwards from the last matching event."
            ),
        },
        "depth": {
            "type": "integer",
            "description": "Maximum number of causal events to return (default 20, max 100).",
        },
        "include_payloads": {
            "type": "boolean",
            "description": "Include full event payloads in the output (default false).",
        },
    },
)
async def causal_trace(
    session_id: str = "",
    run_id: str = "",
    outcome: str = "",
    depth: int = 20,
    include_payloads: bool = False,
) -> str:
    """Return a causal trace of supervisor events leading to an outcome."""
    depth = min(max(1, depth), 100)

    db_path = _get_supervisor_db()
    conn = _open_db(db_path)

    if conn is None:
        return json.dumps(
            {
                "status": "no_db",
                "message": f"Supervisor database not found at {db_path}.",
            },
        )

    try:
        # ── Resolve session / run ────────────────────────────────────────────
        if not session_id:
            rows = _rows(
                conn,
                "SELECT session_id FROM supervisor_runs ORDER BY started_at DESC LIMIT 1",
            )
            session_id = rows[0]["session_id"] if rows else ""

        if not run_id:
            if session_id:
                rows = _rows(
                    conn,
                    "SELECT run_id FROM supervisor_runs WHERE session_id = ? "
                    "ORDER BY started_at DESC LIMIT 1",
                    (session_id,),
                )
            else:
                rows = _rows(
                    conn,
                    "SELECT run_id FROM supervisor_runs ORDER BY started_at DESC LIMIT 1",
                )
            run_id = rows[0]["run_id"] if rows else ""

        if not run_id:
            return json.dumps(
                {
                    "status": "no_events",
                    "session_id": session_id,
                    "run_id": run_id,
                    "message": "No runs found.",
                },
            )

        # ── Fetch all causal events for this run ─────────────────────────────
        placeholders = ",".join("?" * len(_CAUSAL_EVENT_KINDS))
        all_events = _rows(
            conn,
            f"SELECT seq, kind, payload_json, timestamp FROM supervisor_events "
            f"WHERE run_id = ? AND kind IN ({placeholders}) ORDER BY seq ASC",
            (run_id, *_CAUSAL_EVENT_KINDS),
        )

        if not all_events:
            return json.dumps(
                {
                    "status": "no_events",
                    "session_id": session_id,
                    "run_id": run_id,
                    "message": "No causal events found for this run.",
                },
            )

        # ── Find terminal event ───────────────────────────────────────────────
        terminal_idx = len(all_events) - 1
        if outcome:
            outcome_lower = outcome.lower()
            for i in range(len(all_events) - 1, -1, -1):
                ev = all_events[i]
                if outcome_lower in ev["kind"].lower():
                    terminal_idx = i
                    break
                try:
                    payload_str = json.dumps(
                        json.loads(ev.get("payload_json") or "{}"),
                    ).lower()
                    if outcome_lower in payload_str:
                        terminal_idx = i
                        break
                except (json.JSONDecodeError, TypeError):
                    pass

        # ── Walk backwards from terminal event ────────────────────────────────
        start_idx = max(0, terminal_idx - depth + 1)
        chain_events = all_events[start_idx : terminal_idx + 1]

        # ── Identify fork point ───────────────────────────────────────────────
        fork_idx: int | None = None
        for i, ev in enumerate(chain_events):
            kind = ev["kind"]
            if kind in ("drift_detected", "run_failed"):
                fork_idx = i
                break
            if kind == "tool_execution_end":
                try:
                    payload = json.loads(ev.get("payload_json") or "{}")
                    if payload.get("error") or payload.get("success") is False:
                        fork_idx = i
                        break
                except (json.JSONDecodeError, TypeError):
                    pass
            if kind == "memory_gated" and outcome and "memory" in outcome.lower():
                fork_idx = i
                break

        # ── Build result ──────────────────────────────────────────────────────
        trace: list[dict[str, Any]] = []
        for i, ev in enumerate(chain_events):
            entry: dict[str, Any] = {
                "seq": ev["seq"],
                "kind": ev["kind"],
                "timestamp": ev["timestamp"],
                "is_terminal": i == len(chain_events) - 1,
                "is_fork_point": fork_idx is not None and i == fork_idx,
            }
            if include_payloads:
                try:
                    entry["payload"] = json.loads(ev.get("payload_json") or "{}")
                except (json.JSONDecodeError, TypeError):
                    entry["payload"] = {}
            trace.append(entry)

        # ── Statistics ────────────────────────────────────────────────────────
        kind_counts: dict[str, int] = {}
        for ev in chain_events:
            kind_counts[ev["kind"]] = kind_counts.get(ev["kind"], 0) + 1

        result = {
            "status": "ok",
            "session_id": session_id,
            "run_id": run_id,
            "outcome_searched": outcome or "(last event)",
            "terminal_event": chain_events[-1]["kind"] if chain_events else None,
            "fork_point": trace[fork_idx] if fork_idx is not None else None,
            "chain_length": len(trace),
            "total_events_in_run": len(all_events),
            "event_kind_counts": kind_counts,
            "trace": trace,
        }

    finally:
        conn.close()

    return json.dumps(result, default=str, indent=2)


# ---------------------------------------------------------------------------
# policy_probe
# ---------------------------------------------------------------------------


@tool(
    "policy_probe",
    description=(
        "Pre-flight policy check: test whether a tool invocation would be permitted "
        "under the current (or overridden) policy before actually executing it. "
        "Returns a structured verdict with the matching rule, reason, and suggested "
        "alternatives when the call would be denied. Use this to avoid permission "
        "errors at runtime, audit what the policy allows, or dry-run a sensitive "
        "operation."
    ),
    parameters={
        "tool_name": {
            "type": "string",
            "description": "Name of the tool to probe (e.g. 'write_file', 'shell_exec').",
        },
        "args": {
            "type": "object",
            "description": (
                "Arguments that would be passed to the tool. Used for path-based "
                "policy checks (e.g. base_dir restrictions on filesystem tools)."
            ),
        },
        "session_id": {
            "type": "string",
            "description": "Session whose active policy to load. Defaults to most-recent session.",
        },
        "policy_override": {
            "type": "object",
            "description": (
                "Inline policy object to use instead of the DB-stored policy. "
                "Useful for dry-running hypothetical policy configurations. "
                "Expected keys: allow_list (list[str]), deny_list (list[str]), "
                "base_dir (str | null), full_access (bool)."
            ),
        },
        "explain": {
            "type": "boolean",
            "description": "Include a plain-English explanation of the verdict (default true).",
        },
    },
)
async def policy_probe(
    tool_name: str,
    args: dict[str, Any] | None = None,
    session_id: str = "",
    policy_override: dict[str, Any] | None = None,
    explain: bool = True,
) -> str:
    """Evaluate whether a tool invocation would be allowed under the active policy."""
    args = args or {}

    # ── Build policy object ──────────────────────────────────────────────────
    policy: ToolPolicy | None = None
    policy_source = "unknown"

    if policy_override:
        try:
            raw_allow = policy_override.get("allow_list") or []
            raw_deny = policy_override.get("deny_list") or []
            raw_base = policy_override.get("base_dir")
            policy = ToolPolicy(
                name="inline_override",
                allow_list=frozenset(raw_allow) if raw_allow else frozenset(),
                deny_list=frozenset(raw_deny) if raw_deny else frozenset(),
                base_dir=Path(raw_base) if raw_base else None,
                full_access=bool(policy_override.get("full_access", False)),
            )
            policy_source = "inline_override"
        except Exception as exc:
            return json.dumps(
                {"status": "error", "message": f"Invalid policy_override: {exc}"},
            )

    if policy is None:
        db_path = _get_supervisor_db()
        conn = _open_db(db_path)
        if conn is not None:
            try:
                if not session_id:
                    rows = _rows(
                        conn,
                        "SELECT session_id FROM supervisor_runs "
                        "ORDER BY started_at DESC LIMIT 1",
                    )
                    session_id = rows[0]["session_id"] if rows else ""

                if session_id:
                    pv = _rows(
                        conn,
                        "SELECT policy_json FROM policy_versions "
                        "WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
                        (session_id,),
                    )
                    if pv:
                        try:
                            data = json.loads(pv[0].get("policy_json", "{}"))
                            raw_allow = data.get("allow_list") or []
                            raw_deny = data.get("deny_list") or []
                            raw_base = data.get("base_dir")
                            policy = ToolPolicy(
                                name=f"db:{session_id}",
                                allow_list=frozenset(raw_allow)
                                if raw_allow
                                else frozenset(),
                                deny_list=frozenset(raw_deny)
                                if raw_deny
                                else frozenset(),
                                base_dir=Path(raw_base) if raw_base else None,
                                full_access=bool(data.get("full_access", False)),
                            )
                            policy_source = f"db:session={session_id}"
                        except Exception:
                            pass
            finally:
                conn.close()

    if policy is None:
        policy = ToolPolicy(name="default_permissive", full_access=True)
        policy_source = "default_permissive"

    # ── Extract path from args for filesystem checks ─────────────────────────
    path_arg: str | None = None
    if tool_name in _FS_TOOLS:
        for key in ("path", "file_path", "directory", "dir_path", "target"):
            if key in args:
                path_arg = str(args[key])
                break

    # ── Evaluate policy ───────────────────────────────────────────────────────
    eval_args = {"path": path_arg} if path_arg else None
    result = evaluate_policy(policy, tool_name, eval_args)

    # ── Build response ────────────────────────────────────────────────────────
    verdict: dict[str, Any] = {
        "status": "ok",
        "tool_name": tool_name,
        "allowed": result.allowed,
        "reason": result.reason,
        "matched_rule": result.matched_rule,
        "policy_source": policy_source,
        "session_id": session_id or None,
        "is_filesystem_tool": tool_name in _FS_TOOLS,
        "path_checked": path_arg,
    }

    if explain:
        if result.allowed:
            if policy.full_access:
                explanation = f"'{tool_name}' is allowed because full_access=True."
            elif result.matched_rule == "allow_list":
                explanation = f"'{tool_name}' is explicitly in the allow_list."
            elif result.matched_rule == "base_dir":
                explanation = (
                    f"'{tool_name}' is a filesystem tool and the path '{path_arg}' "
                    f"is within the allowed base_dir '{policy.base_dir}'."
                )
            else:
                explanation = f"'{tool_name}' is permitted (no deny rule matched)."
        elif result.matched_rule == "deny_list":
            explanation = (
                f"'{tool_name}' is explicitly in the deny_list and will be blocked."
            )
        elif result.matched_rule == "allow_list":
            explanation = (
                f"'{tool_name}' is not in the allow_list. Only these tools are "
                f"permitted: {sorted(policy.allow_list or [])}."
            )
        elif result.matched_rule == "base_dir":
            explanation = (
                f"'{tool_name}' is a filesystem tool but path '{path_arg}' is "
                f"outside the allowed base_dir '{policy.base_dir}'."
            )
        else:
            explanation = f"'{tool_name}' is denied by policy."
        verdict["explanation"] = explanation

    # ── Suggest alternatives when denied ─────────────────────────────────────
    if not result.allowed and policy.allow_list:
        alternatives: list[str] = []
        for allowed_tool in sorted(policy.allow_list):
            if allowed_tool == tool_name:
                continue
            if allowed_tool in _FS_TOOLS and path_arg:
                alt_result = evaluate_policy(policy, allowed_tool, {"path": path_arg})
                if alt_result.allowed:
                    alternatives.append(allowed_tool)
            elif allowed_tool not in _FS_TOOLS:
                alternatives.append(allowed_tool)
        if alternatives:
            verdict["alternatives"] = alternatives[:10]

    # ── Policy summary ────────────────────────────────────────────────────────
    verdict["policy_summary"] = {
        "full_access": policy.full_access,
        "allow_list": sorted(policy.allow_list) if policy.allow_list else None,
        "deny_list": sorted(policy.deny_list) if policy.deny_list else None,
        "base_dir": policy.base_dir,
    }

    return json.dumps(verdict, default=str, indent=2)
