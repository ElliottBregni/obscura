"""CLI entry points for server-side token revocation.

Wired into the main CLI as ``obscura revoke …``. Complements the
deletion API (which wipes data) — this one cuts live access without
touching persisted content.
"""

from __future__ import annotations

import time

import click

from obscura.auth.revocation import default_blocklist


@click.group(
    name="revoke",
    help="Revoke authenticated sessions (token blocklist).",
)
def revoke_group() -> None:
    """Parent group for session revocation."""


@revoke_group.command("token")
@click.argument("jti")
@click.option(
    "--user-id",
    default="",
    help="Optional user id the token belongs to (recorded for audit).",
)
@click.option(
    "--expires-in",
    default=3600,
    show_default=True,
    help=(
        "Seconds from now until the blocklist entry can be purged "
        "(should cover the token's remaining `exp`)."
    ),
)
@click.option(
    "--reason",
    default="",
    help="Free-text justification recorded with the revocation.",
)
def revoke_token(
    jti: str, user_id: str, expires_in: int, reason: str
) -> None:
    """Revoke a single JTI.

    The JTI is the unique identifier in the token's claims (the `jti`
    claim for JWTs). For API-key-derived sessions, it is whatever
    stable identifier was stashed on ``request.state.token_jti``.
    """
    if not jti.strip():
        click.echo("jti must be non-empty", err=True)
        raise SystemExit(1)
    record = default_blocklist().revoke(
        jti.strip(),
        user_id=user_id,
        expires_at=time.time() + float(expires_in),
        reason=reason,
    )
    click.echo(f"Revoked jti={record.jti}")
    if record.user_id:
        click.echo(f"  user_id  : {record.user_id}")
    click.echo(f"  expires  : {record.expires_at}")
    if record.reason:
        click.echo(f"  reason   : {record.reason}")


@revoke_group.command("user")
@click.argument("user_id")
@click.option(
    "--jti",
    "jtis",
    multiple=True,
    help="JTIs belonging to the user (repeat for multiple).",
)
@click.option(
    "--expires-in",
    default=3600,
    show_default=True,
    help="Seconds from now until entries can be purged.",
)
@click.option("--reason", default="", help="Justification recorded.")
def revoke_user(
    user_id: str,
    jtis: tuple[str, ...],
    expires_in: int,
    reason: str,
) -> None:
    """Revoke every JTI supplied for USER_ID.

    Obscura does not yet maintain a forward index from user_id to
    active JTIs (see R-001 in the risk register — session creation
    needs user_id plumbing). Until that lands, the operator supplies
    the list: look them up in the session event store or the Supabase
    session table.
    """
    if not jtis:
        click.echo("at least one --jti is required", err=True)
        raise SystemExit(1)
    count = default_blocklist().revoke_user(
        user_id.strip(),
        jtis=list(jtis),
        expires_at=time.time() + float(expires_in),
        reason=reason,
    )
    click.echo(f"Revoked {count} token(s) for user_id={user_id}")


@revoke_group.command("list")
@click.argument("user_id")
def list_for_user(user_id: str) -> None:
    """Show non-expired revocations for USER_ID."""
    records = default_blocklist().list_for_user(user_id.strip())
    if not records:
        click.echo(f"No active revocations for user_id={user_id}")
        return
    for r in records:
        click.echo(
            f"{r.jti}  expires={r.expires_at}  reason={r.reason!r}"
        )


@revoke_group.command("purge")
def purge() -> None:
    """Drop expired blocklist entries (housekeeping)."""
    removed = default_blocklist().purge()
    click.echo(f"Purged {removed} expired entries")
