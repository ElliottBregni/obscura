"""obscura.cli.kairos_commands — CLI commands for the Kairos goal runtime.

Commands::

    obscura kairos run "<goal title>" [--description TEXT] [--budget-turns N]
    obscura kairos status [--goal-id ID] [--all]
    obscura kairos pause <goal-id>
    obscura kairos resume <goal-id>
    obscura kairos cancel <goal-id>
    obscura kairos respond <goal-id> <intervention-id> <response>
    obscura kairos goals [--status STATUS]
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import click

from obscura.core.agent_loop_factory import make_agent_loop
from obscura.core.agent_loop_v2 import AgentLoopV2
from obscura.core.auth import resolve_auth
from obscura.core.enums.lifecycle import GoalStatus
from obscura.core.kairos import (
    Kairos,
    GoalBudget,
    KairosEventKind,
)
from obscura.core.kairos.types import KairosConfig
from obscura.core.paths import resolve_obscura_home, resolve_obscura_settings
from obscura.core.tools import ToolRegistry
from obscura.core.enums.agent import Backend
from obscura.providers import CopilotBackend

logger = logging.getLogger(__name__)


def _get_db_path() -> str:
    return str(resolve_obscura_home() / "kairos.db")


def _get_kairos(agent_loop: AgentLoopV2 | None = None) -> Kairos:
    """Instantiate Kairos with the default DB path."""
    auth = resolve_auth(Backend.COPILOT)
    backend = CopilotBackend(auth)
    registry = ToolRegistry()
    loop = agent_loop if agent_loop is not None else make_agent_loop(backend, registry)

    # Read notification recipient from settings so interventions ping iMessage
    settings = _read_settings()
    notification_recipient: str = settings.get("kairos", {}).get(
        "notification_recipient", ""
    ) or settings.get("notification_recipient", "")
    kairos_config = KairosConfig(notification_recipient=notification_recipient)

    return Kairos(
        db_path=_get_db_path(),
        agent_loop=loop,
        backend=backend,
        config=kairos_config,
    )


@click.group(name="kairos")
def kairos_group() -> None:
    """Kairos — autonomous goal runtime. Run goals, track progress."""


@kairos_group.command("run")
@click.argument("title")
@click.option("--description", "-d", default="", help="Goal description")
@click.option("--criteria", "-c", multiple=True, help="Success criteria (repeatable)")
@click.option(
    "--budget-turns", default=0, type=int, help="Max model turns (0=unlimited)"
)
@click.option("--budget-tasks", default=0, type=int, help="Max tasks (0=unlimited)")
@click.option(
    "--budget-seconds",
    default=0.0,
    type=float,
    help="Max wall-clock seconds (0=unlimited)",
)
@click.option("--dry-run", is_flag=True, help="Create goal but don't execute")
def kairos_run(
    title: str,
    description: str,
    criteria: tuple[str, ...],
    budget_turns: int,
    budget_tasks: int,
    budget_seconds: float,
    dry_run: bool,
) -> None:
    """Create and run a goal autonomously.

    Example: obscura kairos run "Audit codebase for security issues"
    """

    async def _run() -> None:
        budget = GoalBudget(
            max_turns=budget_turns,
            max_tasks=budget_tasks,
            max_wall_seconds=budget_seconds,
        )
        kairos = _get_kairos()
        try:
            goal_id = await kairos.create_goal(
                title=title,
                description=description or title,
                success_criteria=list(criteria),
                budget=budget,
            )
            click.echo(f"✓ Goal created: {goal_id}")
            click.echo(f"  Title: {title}")
            if criteria:
                click.echo(f"  Criteria: {', '.join(criteria)}")

            if dry_run:
                click.echo("  (dry-run: not executing)")
                return

            click.echo("\nRunning...\n")
            async for event in kairos.run(goal_id):
                _print_event(event)
        finally:
            await kairos.close()

    asyncio.run(_run())


@kairos_group.command("status")
@click.option("--goal-id", "-g", default="", help="Specific goal ID")
@click.option(
    "--all", "show_all", is_flag=True, help="Show all goals (not just active)"
)
def kairos_status(goal_id: str, show_all: bool) -> None:
    """Show goal status.

    Example: obscura kairos status --all
    """

    async def _run() -> None:
        kairos = _get_kairos()
        try:
            if goal_id:
                goal = kairos.get_goal(goal_id)
                _print_goal(goal, verbose=True, kairos=kairos)
            else:
                status_filter = None if show_all else GoalStatus.ACTIVE
                goals = kairos.list_goals(status=status_filter, limit=50)
                if not goals:
                    click.echo("No goals found.")
                    return
                for g in goals:
                    _print_goal(g, verbose=False, kairos=kairos)
        finally:
            await kairos.close()

    asyncio.run(_run())


@kairos_group.command("pause")
@click.argument("goal_id")
def kairos_pause(goal_id: str) -> None:
    """Pause a running goal."""

    async def _run() -> None:
        kairos = _get_kairos()
        try:
            await kairos.pause(goal_id)
            click.echo(f"⏸  Goal {goal_id} paused.")
        finally:
            await kairos.close()

    asyncio.run(_run())


@kairos_group.command("resume")
@click.argument("goal_id")
def kairos_resume(goal_id: str) -> None:
    """Resume a paused goal."""

    async def _run() -> None:
        kairos = _get_kairos()
        try:
            click.echo(f"▶  Resuming goal {goal_id}...\n")
            async for event in kairos.resume(goal_id):
                _print_event(event)
        finally:
            await kairos.close()

    asyncio.run(_run())


@kairos_group.command("cancel")
@click.argument("goal_id")
@click.confirmation_option(prompt="Are you sure you want to cancel this goal?")
def kairos_cancel(goal_id: str) -> None:
    """Cancel a goal permanently."""

    async def _run() -> None:
        kairos = _get_kairos()
        try:
            await kairos.cancel(goal_id)
            click.echo(f"✗ Goal {goal_id} cancelled.")
        finally:
            await kairos.close()

    asyncio.run(_run())


@kairos_group.command("respond")
@click.argument("goal_id")
@click.argument("intervention_id")
@click.argument("response")
def kairos_respond(goal_id: str, intervention_id: str, response: str) -> None:
    """Respond to a pending intervention (unblocks a goal)."""

    async def _run() -> None:
        kairos = _get_kairos()
        try:
            await kairos.resolve_intervention(goal_id, intervention_id, response)
            click.echo(f"✓ Intervention {intervention_id} resolved.")
        finally:
            await kairos.close()

    asyncio.run(_run())


@kairos_group.command("goals")
@click.option(
    "--status",
    "-s",
    default="",
    help="Filter by status (pending/active/completed/failed/cancelled)",
)
@click.option("--limit", "-n", default=20, help="Max results")
def kairos_goals(status: str, limit: int) -> None:
    """List goals."""

    async def _run() -> None:
        kairos = _get_kairos()
        try:
            status_filter = GoalStatus(status) if status else None
            goals = kairos.list_goals(status=status_filter, limit=limit)
            if not goals:
                click.echo("No goals.")
                return
            for g in goals:
                _print_goal(g, verbose=False, kairos=kairos)
        finally:
            await kairos.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    GoalStatus.PENDING: "○",
    GoalStatus.PLANNING: "◌",
    GoalStatus.ACTIVE: "●",
    GoalStatus.PAUSED: "⏸",
    GoalStatus.BLOCKED: "⊘",
    GoalStatus.COMPLETED: "✓",
    GoalStatus.FAILED: "✗",
    GoalStatus.CANCELLED: "⊘",
}

_EVENT_ICONS = {
    KairosEventKind.GOAL_STARTED: "▶",
    KairosEventKind.GOAL_COMPLETED: "✓",
    KairosEventKind.GOAL_FAILED: "✗",
    KairosEventKind.GOAL_CANCELLED: "⊘",
    KairosEventKind.GOAL_PAUSED: "⏸",
    KairosEventKind.PLAN_CREATED: "📋",
    KairosEventKind.PLAN_REVISED: "🔄",
    KairosEventKind.TASK_STARTED: "  →",
    KairosEventKind.TASK_SUCCEEDED: "  ✓",
    KairosEventKind.TASK_FAILED: "  ✗",
    KairosEventKind.TASK_RETRYING: "  ↺",
    KairosEventKind.CHECKPOINT_CREATED: "💾",
    KairosEventKind.INTERVENTION_RAISED: "⚠",
    KairosEventKind.BUDGET_EXCEEDED: "⛔",
    KairosEventKind.BUDGET_WARNING: "⚡",
    KairosEventKind.HEARTBEAT: "♡",
}


def _print_event(event: Any) -> None:
    icon = _EVENT_ICONS.get(event.kind, "·")
    kind = event.kind.value.replace("_", " ").title()

    # Build a short detail string from payload
    detail = ""
    p = event.payload
    if event.kind == KairosEventKind.PLAN_CREATED:
        detail = f"  ({p.get('task_count', '?')} tasks)"
    elif event.kind == KairosEventKind.TASK_STARTED:
        detail = f"  {p.get('title', '')}"
    elif event.kind == KairosEventKind.TASK_SUCCEEDED:
        ms = p.get("elapsed_ms", 0)
        detail = f"  ({ms}ms)"
    elif event.kind == KairosEventKind.TASK_FAILED:
        err = p.get("error", "")[:60]
        detail = f"  {err}"
    elif event.kind == KairosEventKind.GOAL_COMPLETED:
        detail = f"  ({p.get('tasks_completed', '?')} tasks)"
    elif event.kind == KairosEventKind.GOAL_FAILED:
        err = p.get("error", "")[:60]
        detail = f"  {err}"
    elif event.kind == KairosEventKind.INTERVENTION_RAISED:
        detail = f"  id={p.get('intervention_id', '')[:12]}…"
    elif event.kind == KairosEventKind.BUDGET_EXCEEDED:
        detail = f"  dimension={p.get('dimension', '')}"
    elif event.kind == KairosEventKind.CHECKPOINT_CREATED:
        detail = f"  {p.get('completed', 0)}/{p.get('completed', 0) + p.get('pending', 0)} done"

    click.echo(f"{icon} {kind}{detail}")


def _print_goal(goal: Any, *, verbose: bool, kairos: Kairos) -> None:
    icon = _STATUS_ICONS.get(goal.status, "?")
    short_id = goal.goal_id[:8]
    click.echo(f"{icon} [{short_id}] {goal.title}  ({goal.status.value})")
    if verbose:
        if goal.description and goal.description != goal.title:
            click.echo(f"   Description: {goal.description}")
        if goal.success_criteria:
            click.echo("   Success criteria:")
            for c in goal.success_criteria:
                click.echo(f"     • {c}")
        usage = kairos.get_budget_usage(goal.goal_id)
        if usage.tasks_run > 0:
            click.echo(
                f"   Progress: {usage.tasks_run} tasks, "
                f"{usage.turns_used} turns, "
                f"{int(usage.elapsed_seconds)}s elapsed"
            )
        if goal.created_at:
            click.echo(f"   Created: {goal.created_at.strftime('%Y-%m-%d %H:%M')}")


# --- Kairos CLI global settings helpers (enable/disable/status) ---


def _read_settings() -> dict[str, Any]:
    p = resolve_obscura_settings()
    try:
        if p.exists():
            import json as _json

            parsed: Any = _json.loads(p.read_text(encoding="utf-8"))
            return cast(dict[str, Any], parsed) if isinstance(parsed, dict) else {}
    except Exception:
        logger.debug("suppressed exception in _read_settings", exc_info=True)
        return {}
    return {}


def _write_settings(data: dict[str, Any]) -> None:
    p = resolve_obscura_settings()
    p.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    p.write_text(_json.dumps(data, indent=2), encoding="utf-8")


@kairos_group.command("enable")
def kairos_enable() -> None:
    """Enable Kairos background actions for this home (writes .obscura/settings.json)."""
    cfg = _read_settings()
    kairos_section = cfg.setdefault("kairos", {})
    if isinstance(kairos_section, dict):
        cast(dict[str, Any], kairos_section)["enabled"] = True
    cfg["kairos_enabled"] = True
    _write_settings(cfg)
    click.echo("Kairos enabled for this home (~/.obscura/settings.json updated).")


@kairos_group.command("disable")
def kairos_disable() -> None:
    """Disable Kairos background actions for this home."""
    cfg = _read_settings()
    kairos_section = cfg.setdefault("kairos", {})
    if isinstance(kairos_section, dict):
        cast(dict[str, Any], kairos_section)["enabled"] = False
    cfg["kairos_enabled"] = False
    _write_settings(cfg)
    click.echo("Kairos disabled for this home (~/.obscura/settings.json updated).")


@kairos_group.command("enabled")
def kairos_enabled_status() -> None:
    """Show whether Kairos is enabled for this home."""
    cfg = _read_settings()
    nested = cfg.get("kairos", {})
    nested_enabled = (
        cast(dict[str, Any], nested).get("enabled")
        if isinstance(nested, dict)
        else False
    )
    enabled = bool(cfg.get("kairos_enabled") or nested_enabled)
    click.echo(f"Kairos enabled: {enabled}")
