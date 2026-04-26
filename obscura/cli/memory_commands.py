"""obscura.cli.memory_commands — ``obscura memory <subcommand>`` CLI.

Phase 5 ships ``obscura memory backfill-graph``: walk the canonical vector
store and ingest every chunk that lacks ``lr_indexed_at`` into LightRAG's
graph + secondary vector store. Cost-gated, dry-runnable, resumable.
"""

from __future__ import annotations

import json as _json
import os
import sys
from pathlib import Path

import click


def _build_cli_user():
    """Same ad-hoc local user the REPL uses (cli/__init__.py:967)."""
    from obscura.auth.models import AuthenticatedUser

    return AuthenticatedUser(
        user_id=os.environ.get("USER", "local"),
        email="cli@obscura.local",
        roles=("operator",),
        org_id="local",
        token_type="user",
        raw_token="",
    )


def _print_estimate(estimate, config) -> None:
    click.echo("Backfill plan")
    click.echo("─────────────")
    click.echo(f"  total chunks:   {estimate.total_chunks}")
    for mt, n in sorted(estimate.by_memory_type.items()):
        click.echo(f"    {mt}: {n}")
    click.echo(f"  estimated LLM:  {estimate.estimated_llm_calls} calls")
    click.echo(f"  estimated cost: ${estimate.estimated_cost_usd:.2f} USD")
    click.echo(f"  rate limit:     {config.rate_limit} chunks/sec")
    click.echo(
        f"  estimated wall: {int(estimate.estimated_duration_seconds // 60)}m "
        f"{int(estimate.estimated_duration_seconds % 60)}s"
    )


def _print_report(report, estimate) -> None:
    click.echo("\nBackfill complete")
    click.echo("─────────────────")
    click.echo(f"  duration:       {report.duration_seconds:.1f}s")
    click.echo(f"  indexed:        {report.chunks_indexed} / {estimate.total_chunks}")
    click.echo(f"  failed:         {report.chunks_failed}")
    click.echo(f"  actual LLM:     {report.actual_llm_calls} calls")
    click.echo(f"  actual cost:    ${report.actual_cost_usd:.2f} USD")
    if report.failed_keys:
        click.echo("\n  first failures:")
        for ns, k, err in report.failed_keys[:3]:
            click.echo(f"    {ns}::{k} — {err}")


@click.group(name="memory")
def memory_group() -> None:
    """Memory backfill, statistics, and maintenance."""


@memory_group.command("backfill-graph")
@click.option(
    "--user",
    default=None,
    help="User id override (default: $USER, matching REPL conventions)",
)
@click.option("--namespace", default=None, help="Filter by namespace")
@click.option(
    "--memory-types",
    default=None,
    help="Comma-separated memory types (default: indexable types from config)",
)
@click.option("--batch-size", default=50, type=int)
@click.option("--rate-limit", default=1.0, type=float, help="Chunks per second")
@click.option("--max-chunks", default=None, type=int)
@click.option("--dry-run", is_flag=True, help="Estimate only; no LLM calls")
@click.option("--confirm", is_flag=True, help="Required for non-TTY runs > $1.00")
@click.option(
    "--resume", is_flag=True, help="Resume an interrupted run (semantic alias)"
)
@click.option(
    "--retry-failed",
    is_flag=True,
    help="Re-attempt chunks with lr_index_attempts > 0",
)
@click.option("--include-episodes", is_flag=True, help="Include episode-type chunks")
@click.option("--json", "as_json", is_flag=True, help="JSON output")
def backfill_graph(
    user: str | None,
    namespace: str | None,
    memory_types: str | None,
    batch_size: int,
    rate_limit: float,
    max_chunks: int | None,
    dry_run: bool,
    confirm: bool,
    resume: bool,  # noqa: ARG001  # explicit semantic alias; behavior is naturally idempotent
    retry_failed: bool,
    include_episodes: bool,
    as_json: bool,
) -> None:
    """Index existing chunks into the LightRAG knowledge graph."""
    from obscura.lightrag_memory import _lightrag_enabled
    from obscura.lightrag_memory.backfill import (
        BackfillConfig,
        BackfillEngine,
        _backfill_lock_path,
    )
    from obscura.lightrag_memory.hybrid_store import HybridVectorMemoryStore
    from obscura.vector_memory import VectorMemoryStore

    if not _lightrag_enabled():
        msg = (
            "LightRAG is disabled. Set OBSCURA_LIGHTRAG=on and install "
            "the extra: `uv sync --extra lightrag`."
        )
        raise click.ClickException(msg)

    auth_user = _build_cli_user()
    if user:
        from obscura.auth.models import AuthenticatedUser

        auth_user = AuthenticatedUser(
            user_id=user,
            email=auth_user.email,
            roles=auth_user.roles,
            org_id=auth_user.org_id,
            token_type=auth_user.token_type,
            raw_token=auth_user.raw_token,
        )

    store = VectorMemoryStore.for_user(auth_user)
    if not isinstance(store, HybridVectorMemoryStore):
        msg = (
            "VectorMemoryStore.for_user did not return a HybridVectorMemoryStore. "
            "Verify OBSCURA_LIGHTRAG=on and that Phase-4 wiring (for_user dispatch) "
            "has landed."
        )
        raise click.ClickException(msg)

    config = BackfillConfig(
        namespace=namespace,
        memory_types=frozenset(memory_types.split(",")) if memory_types else None,
        batch_size=batch_size,
        rate_limit=rate_limit,
        max_chunks=max_chunks,
        dry_run=dry_run,
        retry_failed=retry_failed,
        include_episodes=include_episodes,
    )

    engine = BackfillEngine(store, config)
    estimate = engine.estimate()

    if as_json:
        click.echo(_json.dumps({"estimate": estimate.to_dict(), "dry_run": dry_run}))
    else:
        _print_estimate(estimate, config)

    if dry_run:
        if not as_json:
            click.echo("\nNO CHANGES MADE. Re-run without --dry-run to execute.")
        return

    threshold = float(os.environ.get("OBSCURA_LR_BACKFILL_COST_THRESHOLD_USD", "1.00"))
    if estimate.estimated_cost_usd > threshold:
        if sys.stdin.isatty():
            click.confirm(
                f"Estimated cost: ${estimate.estimated_cost_usd:.2f}. Continue?",
                abort=True,
            )
        elif not confirm:
            msg = (
                f"Estimated cost: ${estimate.estimated_cost_usd:.2f} exceeds "
                f"non-TTY threshold (${threshold:.2f}). Pass --confirm to proceed."
            )
            raise click.ClickException(msg)

    lock_path: Path = _backfill_lock_path(auth_user)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            msg = (
                "Another backfill is in progress for this user.\n"
                f"Lock file: {lock_path}\n"
                "If you're sure no backfill is running, delete the lock and retry."
            )
            raise click.ClickException(msg) from exc

        report = engine.run()
        if as_json:
            click.echo(_json.dumps({"report": report.to_dict()}))
        else:
            _print_report(report, estimate)
    finally:
        os.close(fd)
