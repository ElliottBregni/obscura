fpath = "/Users/elliottbregni/dev/obscura-main/obscura/cli/__init__.py"
with open(fpath, "r", encoding="utf-8") as f:
    content = f.read()

marker = "# ---------------------------------------------------------------------------\n# channels command group\n# ---------------------------------------------------------------------------"

if marker not in content:
    print("ERROR: marker not found")
    exit(1)

INSERT = r'''# ---------------------------------------------------------------------------
# kairos command group
# ---------------------------------------------------------------------------


@main.group()
def kairos() -> None:
    """Inspect and control the KAIROS autonomous background daemon."""


@kairos.command("status")
def kairos_status() -> None:
    """Show KAIROS engine status, daily log stats, and active goals."""
    import datetime

    from obscura.kairos.engine import KairosEngine, is_kairos_enabled

    enabled = is_kairos_enabled()
    click.echo(
        f"\nKAIROS enabled : {'yes' if enabled else 'no  (set OBSCURA_KAIROS=1 to enable)'}"
    )

    if not enabled:
        click.echo()
        return

    try:
        engine = KairosEngine()
        st = engine.status()
        click.echo(f"Running        : {'yes' if st['running'] else 'no (starts with chat session)'}")
        click.echo(f"Observations   : {st['observations']}")
        click.echo(f"Ticks          : {st['tick_count']}")
        click.echo(f"Proactive      : {'on' if st['proactive_enabled'] else 'off'}")
        click.echo(f"Dream          : {'on' if st['dream_enabled'] else 'off'}")
        click.echo(f"Log entries    : {st['daily_log_entries']}  ({st['daily_log_path']})")
    except Exception as exc:
        click.echo(f"[engine status error: {exc}]", err=True)

    try:
        from obscura.kairos.dream import DreamConsolidator
        dc = DreamConsolidator()
        ready = dc.should_run()
        click.echo(
            f"Dream ready    : {'yes - will run at session end' if ready else 'no - gates not met'}"
        )
    except Exception:
        pass

    try:
        from obscura.kairos.goals import GoalBoard
        board = GoalBoard()
        goals = board.active_goals()
        if goals:
            click.echo(f"\nActive goals ({len(goals)}):")
            for g in goals:
                bar = "#" * (g.progress // 10) + "." * (10 - g.progress // 10)
                stale = ""
                if g.last_worked:
                    try:
                        lw = datetime.date.fromisoformat(g.last_worked)
                        days_idle = (datetime.date.today() - lw).days
                        stale = f"  [idle {days_idle}d]" if days_idle >= 3 else ""
                    except Exception:
                        pass
                click.echo(
                    f"  [{g.priority:8s}] {g.progress:3d}%  {g.title[:60]}{stale}"
                )
        else:
            click.echo("\nNo active goals.")
    except Exception as exc:
        click.echo(f"[goals error: {exc}]", err=True)

    click.echo()


@kairos.command("logs")
@click.option("--days", "-d", default=1, type=int, show_default=True, help="Days of logs to show.")
@click.option("--tail", "-n", default=0, type=int, help="Show last N entries only (0 = all).")
@click.option(
    "--source",
    default=None,
    help="Filter by source: kairos, tool, user, agent, dream.",
)
def kairos_logs(days: int, tail: int, source: str | None) -> None:
    """Show KAIROS daily observation logs."""
    from obscura.kairos.daily_log import DailyLog

    log_paths = DailyLog.recent_logs(days)
    if not log_paths:
        click.echo(f"No logs found for the last {days} day(s).")
        click.echo("Logs live at: ~/.obscura/memory/logs/YYYY/MM/YYYY-MM-DD.md")
        click.echo("They are written during active chat sessions when KAIROS is enabled.")
        return

    for path in reversed(log_paths):
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()

        if source:
            lines = [ln for ln in lines if f"({source})" in ln or ln.startswith("#")]

        if tail > 0:
            header = [ln for ln in lines if ln.startswith("#")]
            entries = [ln for ln in lines if ln.startswith("- [")]
            lines = header + entries[-tail:]

        click.echo(f"\n{'-' * 60}")
        click.echo(f"  {path.name}")
        click.echo(f"{'-' * 60}")
        click.echo("\n".join(lines))

    click.echo()


@kairos.command("dream")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Skip gate checks and run consolidation now.",
)
def kairos_dream(force: bool) -> None:
    """Run dream consolidation (memory + goals + user profile update).

    Normal mode: only runs if 24h+ elapsed and 5+ sessions since last run.
    Force mode : skips all gates and runs immediately.
    """
    import asyncio

    async def _run() -> None:
        from obscura.kairos.dream import DreamConsolidator

        dc = DreamConsolidator(
            min_hours=0.0 if force else 24.0,
            min_sessions=0 if force else 5,
        )

        if not force and not dc.should_run():
            click.echo(
                "Dream gates not met (< 24h elapsed or < 5 sessions since last run).\n"
                "Use --force to run anyway."
            )
            return

        click.echo("Starting dream consolidation...")
        click.echo("This may take 1-3 minutes (up to 15 agent turns).")
        click.echo("Updating: memory files, goal progress, user profile.\n")

        try:
            ok = await dc.run()
            if ok:
                click.echo("Dream consolidation complete.")
                click.echo("Check ~/.obscura/memory/ for updated files.")
            else:
                click.echo("Consolidation failed or was already running.", err=True)
        except Exception as exc:
            click.echo(f"Error: {exc}", err=True)

    asyncio.run(_run())


@kairos.command("goals")
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Show all goals including completed/abandoned.",
)
def kairos_goals_cmd(show_all: bool) -> None:
    """List goals from the KAIROS goal board."""
    import datetime

    try:
        from obscura.kairos.goals import GoalBoard
    except ImportError as exc:
        click.echo(f"Error: {exc}", err=True)
        return

    board = GoalBoard()
    goals = board.load_all() if show_all else board.active_goals()

    if not goals:
        click.echo("No goals found." if show_all else "No active goals. Use --all to see all.")
        return

    click.echo()
    for g in goals:
        icon = {"completed": "V", "abandoned": "X", "draft": "o"}.get(g.status, "*")
        bar = "#" * (g.progress // 10) + "." * (10 - g.progress // 10)
        stale = ""
        if g.last_worked:
            try:
                lw = datetime.date.fromisoformat(g.last_worked)
                days_idle = (datetime.date.today() - lw).days
                stale = f"  <- idle {days_idle}d" if days_idle >= 3 else ""
            except Exception:
                pass
        click.echo(
            f"  {icon} [{g.priority:8s}] [{g.status:11s}] {g.progress:3d}%"
            f"  {g.title}{stale}"
        )
        for ac in list(g.acceptance_criteria)[:3]:
            click.echo(f"      - {ac[:80]}")
    click.echo()


'''

new_content = content.replace(marker, INSERT + marker, 1)

if new_content == content:
    print("ERROR: replacement had no effect")
    exit(1)

with open(fpath, "w", encoding="utf-8") as f:
    f.write(new_content)

print("SUCCESS: kairos group inserted")
print(f"File size: {len(new_content)} bytes")
