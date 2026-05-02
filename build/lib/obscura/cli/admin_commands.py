"""CLI entry points for administrative operations.

Wired into the main CLI as ``obscura admin ...``. Currently only exposes
user-data deletion, the SOC2 C1/P6 control. Future additions
(bulk export, retention purge triggers) belong here.

All commands are intentionally confirmation-gated — these are
destructive operations that no prompt-driven flow should ever invoke
without an operator on the keyboard.
"""

from __future__ import annotations

import json

import click

from obscura.admin import delete_user_data


@click.group(name="admin", help="Administrative operations (destructive — use with care).")
def admin_group() -> None:
    """Parent group for destructive administrative operations."""


@admin_group.command("delete-user")
@click.argument("user_id")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report what would be deleted without touching anything.",
)
@click.option(
    "--yes",
    "skip_confirm",
    is_flag=True,
    default=False,
    help="Skip the interactive confirmation. Intended for non-interactive scripts.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the deletion receipt as JSON (useful for programmatic callers).",
)
def delete_user(
    user_id: str,
    *,
    dry_run: bool,
    skip_confirm: bool,
    as_json: bool,
) -> None:
    """Erase every persistent trace of USER_ID across all stores.

    Tombstones the action in the audit log instead of removing prior
    audit records — required for SOC2 CC2 audit integrity.

    \b
    Examples:
        obscura admin delete-user alice@example.com --dry-run
        obscura admin delete-user alice@example.com --yes
    """
    if not dry_run and not skip_confirm:
        click.echo(
            f"About to erase all data belonging to user_id={user_id!r}.\n"
            "This includes memory, vector memory, session history, kairos\n"
            "goals, and queued notifications. An audit tombstone will be\n"
            "appended; the action is otherwise irreversible.\n"
        )
        if not click.confirm("Proceed?", default=False):
            click.echo("Aborted — nothing deleted.")
            raise SystemExit(1)

    receipt = delete_user_data(user_id, dry_run=dry_run)

    if as_json:
        click.echo(
            json.dumps(
                {
                    "user_id": receipt.user_id,
                    "user_hash": receipt.user_hash,
                    "dry_run": receipt.dry_run,
                    "ok": receipt.ok(),
                    "total_records": receipt.total_records(),
                    "started_at": receipt.started_at,
                    "finished_at": receipt.finished_at,
                    "per_store": receipt.per_store,
                },
                indent=2,
                default=str,
            )
        )
        return

    verb = "Would delete" if receipt.dry_run else "Deleted"
    click.echo(f"{verb} data for user_id={user_id!r} (hash={receipt.user_hash})")
    click.echo(f"  started : {receipt.started_at}")
    click.echo(f"  finished: {receipt.finished_at}")
    click.echo(f"  total records touched: {receipt.total_records()}")
    click.echo("")
    for name, step in receipt.per_store.items():
        marker = "✗" if step.get("error") else "✓"
        click.echo(f"  {marker} {name}: {_format_step(step)}")

    if not receipt.ok():
        raise SystemExit(2)


def _format_step(step: dict[str, object]) -> str:
    if step.get("error"):
        return f"error: {step['error']}"
    parts: list[str] = []
    if "records" in step:
        parts.append(f"records={step['records']}")
    if "events" in step:
        parts.append(f"events={step['events']}")
    if "per_table" in step:
        parts.append(f"per_table={step['per_table']}")
    if "collection" in step:
        parts.append(f"collection={step['collection']}")
    if "bytes" in step:
        parts.append(f"bytes={step['bytes']}")
    if "note" in step:
        parts.append(str(step["note"]))
    if step.get("dry_run"):
        parts.append("(dry-run)")
    return ", ".join(parts) if parts else "no-op"
