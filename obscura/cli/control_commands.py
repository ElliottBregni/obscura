"""obscura.cli.control_commands — /heartbeat, /status, /policies, /replay."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from obscura.cli.render import (
    ACCENT,
    ACCENT_DIM,
    CODE_THEME,
    ERROR_COLOR,
    OK_COLOR,
    TOOL_COLOR,
    WARN_COLOR,
    console,
    print_error,
    print_info,
)
from obscura.core.paths import resolve_obscura_home

if TYPE_CHECKING:
    from obscura.cli.commands import REPLContext


# ---------------------------------------------------------------------------
# HeartbeatReport dataclass
# ---------------------------------------------------------------------------


@dataclass
class HeartbeatReport:
    """Snapshot of session health collected by /heartbeat."""

    timestamp: str = ""
    latency_ms: float = 0.0

    # Session
    session_id: str = ""
    session_status: str = ""
    session_backend: str = ""
    session_model: str = ""
    message_count: int = 0
    event_count: int = 0

    # Memory (placeholder for future use)
    memory_total_keys: int = 0
    memory_expired_keys: int = 0
    memory_namespaces: int = 0

    # Vector memory
    vector_memory_count: int = 0
    vector_memory_backend: str = ""

    # Tools
    tools_enabled: bool = False
    tool_count: int = 0
    tool_names: list[str] = field(default_factory=list)

    # Events DB
    events_db_ok: bool = False
    events_db_size_kb: float = 0.0

    # Supervisor
    supervisor_db_exists: bool = False
    supervisor_lock_held: bool = False
    supervisor_lock_holder: str = ""
    supervisor_state: str = ""
    supervisor_heartbeat_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict."""
        return {
            "timestamp": self.timestamp,
            "latency_ms": self.latency_ms,
            "session_id": self.session_id,
            "session_status": self.session_status,
            "session_backend": self.session_backend,
            "session_model": self.session_model,
            "message_count": self.message_count,
            "event_count": self.event_count,
            "memory_total_keys": self.memory_total_keys,
            "memory_expired_keys": self.memory_expired_keys,
            "memory_namespaces": self.memory_namespaces,
            "vector_memory_count": self.vector_memory_count,
            "vector_memory_backend": self.vector_memory_backend,
            "tools_enabled": self.tools_enabled,
            "tool_count": self.tool_count,
            "tool_names": self.tool_names,
            "events_db_ok": self.events_db_ok,
            "events_db_size_kb": self.events_db_size_kb,
            "supervisor_db_exists": self.supervisor_db_exists,
            "supervisor_lock_held": self.supervisor_lock_held,
            "supervisor_lock_holder": self.supervisor_lock_holder,
            "supervisor_state": self.supervisor_state,
            "supervisor_heartbeat_count": self.supervisor_heartbeat_count,
        }

    def to_json(self) -> str:
        """Serialize to indented JSON string."""
        return json.dumps(self.to_dict(), indent=2)


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------


async def _collect_heartbeat(ctx: REPLContext) -> HeartbeatReport:
    """Gather health data from every subsystem, tolerating failures."""
    t0 = time.monotonic()
    report = HeartbeatReport(
        timestamp=datetime.now(UTC).isoformat(),
        session_id=ctx.session_id,
        session_backend=ctx.backend,
        session_model=ctx.model or "",
        tools_enabled=ctx.tools_enabled,
    )

    # 1. Session record
    try:
        rec = await ctx.store.get_session(ctx.session_id)
        if rec is not None:
            report.session_status = rec.status.value if hasattr(rec.status, "value") else str(rec.status)
            report.message_count = rec.message_count
    except Exception:
        pass

    # 2. Event count
    try:
        events = await ctx.store.get_events(ctx.session_id)
        report.event_count = len(events)
    except Exception:
        pass

    # 3. Events DB health
    try:
        db_path: Path = ctx.store._db_path  # type: ignore[attr-defined]
        if db_path.exists():
            report.events_db_ok = True
            report.events_db_size_kb = db_path.stat().st_size / 1024.0
    except Exception:
        pass

    # 4. Tool registry
    try:
        tools = ctx.client.list_tools()
        report.tool_count = len(tools)
        report.tool_names = [t.name for t in tools]
    except Exception:
        pass

    # 5. Vector memory
    try:
        if ctx.vector_store is not None:
            stats = ctx.vector_store.get_stats()
            report.vector_memory_count = stats.get("total_memories", 0)
            report.vector_memory_backend = stats.get("backend", "")
    except Exception:
        pass

    # 6. Supervisor health (sync probe via thread)
    try:
        await asyncio.to_thread(_probe_supervisor_sync, report, ctx.session_id)
    except Exception:
        pass

    report.latency_ms = (time.monotonic() - t0) * 1000.0
    return report


def _probe_supervisor_sync(report: HeartbeatReport, session_id: str) -> None:
    """Read supervisor.db tables using raw sqlite3 (no schema creation)."""
    db_path = resolve_obscura_home() / "supervisor.db"
    if not db_path.exists():
        return
    report.supervisor_db_exists = True

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        # Lock status
        try:
            row = conn.execute(
                "SELECT holder_id, expires_at FROM session_locks WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is not None:
                expires = row["expires_at"]
                now_iso = datetime.now(UTC).isoformat()
                if expires > now_iso:
                    report.supervisor_lock_held = True
                    report.supervisor_lock_holder = row["holder_id"]
        except Exception:
            pass

        # Latest heartbeat state + count
        try:
            row = conn.execute(
                "SELECT state, COUNT(*) as cnt FROM session_heartbeats "
                "WHERE session_id = ? GROUP BY session_id ORDER BY seq DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if row is not None:
                report.supervisor_state = row["state"]
                report.supervisor_heartbeat_count = row["cnt"]
        except Exception:
            # Try simpler queries if the above fails
            try:
                count_row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM session_heartbeats WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if count_row is not None:
                    report.supervisor_heartbeat_count = count_row["cnt"]
            except Exception:
                pass
            try:
                state_row = conn.execute(
                    "SELECT state FROM session_heartbeats "
                    "WHERE session_id = ? ORDER BY seq DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
                if state_row is not None:
                    report.supervisor_state = state_row["state"]
            except Exception:
                pass

        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Rich rendering
# ---------------------------------------------------------------------------


def _render_heartbeat_rich(report: HeartbeatReport) -> None:
    """Render a HeartbeatReport as a Rich panel with a table."""
    table = Table(show_header=False, box=None, padding=(0, 1), expand=False)
    table.add_column("key", style="bold", width=18)
    table.add_column("value")

    # Session section
    sid_short = report.session_id[:16] if len(report.session_id) > 16 else report.session_id
    status = report.session_status or "unknown"
    if status == "active":
        status_display = f"[{OK_COLOR}]{status}[/]"
    elif status in ("failed", "error"):
        status_display = f"[{ERROR_COLOR}]{status}[/]"
    else:
        status_display = f"[{WARN_COLOR}]{status}[/]"

    table.add_row("session", f"[{ACCENT}]{sid_short}[/]  {status_display}")
    table.add_row("backend", f"[{ACCENT_DIM}]{report.session_backend}[/]")
    table.add_row("model", f"[{ACCENT_DIM}]{report.session_model or 'default'}[/]")
    table.add_row("messages", str(report.message_count))
    table.add_row("events", str(report.event_count))

    # Tools
    tools_status = f"[{OK_COLOR}]on[/]" if report.tools_enabled else f"[{WARN_COLOR}]off[/]"
    table.add_row("tools", f"{tools_status}  [{TOOL_COLOR}]{report.tool_count}[/] registered")

    # Vector memory
    if report.vector_memory_count > 0 or report.vector_memory_backend:
        table.add_row(
            "vector memory",
            f"{report.vector_memory_count} items  [{ACCENT_DIM}]{report.vector_memory_backend}[/]",
        )

    # DB health
    if report.events_db_ok:
        db_display = f"[{OK_COLOR}]ok[/]  {report.events_db_size_kb:.1f} KB"
    else:
        db_display = f"[{ERROR_COLOR}]missing[/]"
    table.add_row("events db", db_display)

    # Supervisor (only if DB exists)
    if report.supervisor_db_exists:
        if report.supervisor_lock_held:
            lock_display = f"[{WARN_COLOR}]held[/] by {report.supervisor_lock_holder[:16]}"
        else:
            lock_display = f"[{OK_COLOR}]free[/]"
        table.add_row("supervisor lock", lock_display)

        if report.supervisor_state:
            table.add_row("supervisor state", f"[{ACCENT_DIM}]{report.supervisor_state}[/]")
        if report.supervisor_heartbeat_count > 0:
            table.add_row("heartbeats", str(report.supervisor_heartbeat_count))

    # Latency footer
    latency_color = OK_COLOR if report.latency_ms < 200 else WARN_COLOR
    table.add_row("latency", f"[{latency_color}]{report.latency_ms:.0f} ms[/]")

    console.print(
        Panel(
            table,
            title=f"[bold {ACCENT}]heartbeat[/]",
            title_align="left",
            border_style=ACCENT_DIM,
            expand=False,
            padding=(0, 1),
        )
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def cmd_heartbeat(args: str, ctx: REPLContext) -> str | None:
    """Collect and display session health metrics."""
    report = await _collect_heartbeat(ctx)

    if args.strip() == "--json":
        console.print(
            Syntax(report.to_json(), "json", theme=CODE_THEME, word_wrap=True)
        )
    else:
        _render_heartbeat_rich(report)

    return None


async def cmd_status(args: str, ctx: REPLContext) -> str | None:
    """Alias for /heartbeat."""
    return await cmd_heartbeat(args, ctx)


async def cmd_policies(args: str, ctx: REPLContext) -> str | None:
    """List policy versions from supervisor.db."""
    db_path = resolve_obscura_home() / "supervisor.db"
    if not db_path.exists():
        print_info("No supervisor.db found.")
        return None

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT policy_id, scope, scope_id, version, hash, created_at "
            "FROM policy_versions ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
    except Exception as exc:
        print_error(f"Failed to query policy_versions: {exc}")
        return None

    if not rows:
        print_info("No policies found.")
        return None

    table = Table(title="Policy Versions", expand=False)
    table.add_column("policy_id", style=ACCENT, max_width=20, no_wrap=True)
    table.add_column("scope", style="bold")
    table.add_column("scope_id", max_width=16, no_wrap=True)
    table.add_column("version", justify="right")
    table.add_column("hash", style="dim", max_width=12, no_wrap=True)
    table.add_column("created_at", style="dim")

    for row in rows:
        table.add_row(
            row["policy_id"][:20],
            row["scope"],
            row["scope_id"][:16] if row["scope_id"] else "",
            str(row["version"]),
            row["hash"][:12],
            row["created_at"],
        )

    console.print(table)
    return None


async def cmd_replay(args: str, ctx: REPLContext) -> str | None:
    """Replay supervisor run events for a given run_id prefix."""
    run_prefix = args.strip()
    if not run_prefix:
        print_error("Usage: /replay <run_id_prefix>")
        return None

    db_path = resolve_obscura_home() / "supervisor.db"
    if not db_path.exists():
        print_info("No supervisor.db found.")
        return None

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        # Find matching run
        run_row = conn.execute(
            "SELECT run_id, session_id, agent_id, state, turn_count, "
            "started_at, completed_at, error "
            "FROM supervisor_runs WHERE run_id LIKE ? ORDER BY started_at DESC LIMIT 1",
            (f"{run_prefix}%",),
        ).fetchone()

        if run_row is None:
            conn.close()
            print_error(f"No run matching prefix '{run_prefix}'.")
            return None

        run_id = run_row["run_id"]

        # Fetch events
        event_rows = conn.execute(
            "SELECT seq, kind, payload, timestamp "
            "FROM supervisor_events WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
        conn.close()
    except Exception as exc:
        print_error(f"Failed to query supervisor DB: {exc}")
        return None

    # Render run info
    info_table = Table(show_header=False, box=None, padding=(0, 1), expand=False)
    info_table.add_column("key", style="bold", width=14)
    info_table.add_column("value")

    info_table.add_row("run_id", f"[{ACCENT}]{run_id}[/]")
    info_table.add_row("session_id", run_row["session_id"][:16] if run_row["session_id"] else "")
    info_table.add_row("agent_id", run_row["agent_id"] or "")
    state_val = run_row["state"]
    if state_val == "completed":
        state_display = f"[{OK_COLOR}]{state_val}[/]"
    elif state_val in ("failed", "error"):
        state_display = f"[{ERROR_COLOR}]{state_val}[/]"
    else:
        state_display = f"[{WARN_COLOR}]{state_val}[/]"
    info_table.add_row("state", state_display)
    info_table.add_row("turns", str(run_row["turn_count"]))
    info_table.add_row("started", run_row["started_at"] or "")
    info_table.add_row("completed", run_row["completed_at"] or "")
    if run_row["error"]:
        info_table.add_row("error", f"[{ERROR_COLOR}]{run_row['error']}[/]")

    console.print(
        Panel(
            info_table,
            title=f"[bold {ACCENT}]run[/]",
            title_align="left",
            border_style=ACCENT_DIM,
            expand=False,
            padding=(0, 1),
        )
    )

    # Render events
    if not event_rows:
        print_info("No events recorded for this run.")
        return None

    ev_table = Table(title=f"Events ({len(event_rows)})", expand=False)
    ev_table.add_column("seq", justify="right", style="dim", width=5)
    ev_table.add_column("kind", style=TOOL_COLOR, max_width=24)
    ev_table.add_column("payload", max_width=60, no_wrap=True)
    ev_table.add_column("timestamp", style="dim", max_width=26)

    for row in event_rows:
        payload_str = row["payload"]
        if len(payload_str) > 60:
            payload_str = payload_str[:57] + "..."
        ev_table.add_row(
            str(row["seq"]),
            row["kind"],
            payload_str,
            row["timestamp"],
        )

    console.print(ev_table)
    return None
