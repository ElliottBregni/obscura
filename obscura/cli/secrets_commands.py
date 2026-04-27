"""CLI entry points for OS-keyring-backed secret management.

Wired into the main CLI as ``obscura secrets …``. Operators who want to
move their LLM credentials out of environment variables store them
here; the provider resolvers pick them up transparently (see
``obscura.core.auth._keyring_secret``) with priority after explicit
caller values and before env vars (in the default oauth_first mode).
"""

from __future__ import annotations

import click

from obscura.core.secret_store import (
    KNOWN_SECRETS,
    delete_secret,
    get_secret,
    is_available,
    list_stored,
    set_secret,
)


@click.group(
    name="secrets",
    help="Manage provider credentials stored in the OS keyring.",
)
def secrets_group() -> None:
    """Parent group for keyring-backed secret management."""


@secrets_group.command("status")
def status() -> None:
    """Show whether the OS keyring is usable and what's currently stored."""
    if not is_available():
        click.echo(
            "OS keyring is NOT available on this system.\n"
            "Install the 'encrypted' extra (uv pip install 'obscura[encrypted]')\n"
            "or a platform keyring backend to use keyring-backed secrets.\n"
            "Obscura will continue reading credentials from environment variables."
        )
        raise SystemExit(1)

    stored = set(list_stored())
    click.echo("OS keyring is available.\n")
    click.echo("Known credential slots:")
    for name, label in KNOWN_SECRETS:
        marker = "✓" if name in stored else " "
        click.echo(f"  [{marker}] {name:<24}  {label}")
    click.echo("")
    click.echo(
        "Set one with: obscura secrets set <name>\n"
        "Remove one with: obscura secrets delete <name>"
    )


@secrets_group.command("set")
@click.argument("name")
@click.option(
    "--value",
    default=None,
    help=(
        "Secret value. When omitted, prompts securely on stdin so the value "
        "doesn't land in shell history."
    ),
)
def set_cmd(name: str, value: str | None) -> None:
    """Store VALUE for NAME in the OS keyring.

    \b
    Known NAMEs:
        github:token, anthropic:api_key, openai:api_key,
        moonshot:api_key, obscura:db_key
    """
    if not is_available():
        click.echo("OS keyring not available; nothing stored.", err=True)
        raise SystemExit(1)
    if value is None:
        value = click.prompt(
            f"Enter value for {name}",
            hide_input=True,
            confirmation_prompt=False,
        )
    if not value.strip():
        click.echo("Empty value; refusing to store.", err=True)
        raise SystemExit(1)
    ok = set_secret(name, value)
    if not ok:
        click.echo(f"Failed to store {name}.", err=True)
        raise SystemExit(2)
    # Never echo the value itself — SOC2 CC2 log-leak risk.
    click.echo(f"Stored {name} in the OS keyring.")


@secrets_group.command("delete")
@click.argument("name")
@click.option(
    "--yes",
    "skip_confirm",
    is_flag=True,
    default=False,
    help="Skip confirmation.",
)
def delete_cmd(name: str, skip_confirm: bool) -> None:
    """Remove NAME from the OS keyring."""
    if not is_available():
        click.echo("OS keyring not available; nothing to do.", err=True)
        raise SystemExit(1)
    if not skip_confirm and not click.confirm(
        f"Remove {name} from the OS keyring?", default=False
    ):
        click.echo("Aborted.")
        raise SystemExit(1)
    removed = delete_secret(name)
    if removed:
        click.echo(f"Removed {name}.")
    else:
        click.echo(f"No stored value for {name} (or removal failed).")


@secrets_group.command("list")
def list_cmd() -> None:
    """Show which KNOWN slots have values set. Never shows values."""
    stored = list_stored()
    if not stored:
        click.echo("No secrets stored in the keyring.")
        return
    for name in stored:
        click.echo(name)


@secrets_group.command("show")
@click.argument("name")
def show_cmd(name: str) -> None:
    """Check whether NAME has a stored value (does NOT print the secret)."""
    value = get_secret(name)
    if value:
        # Never echo the secret itself. Show a fingerprint that's useful
        # for confirming the right value is stored without leaking the value.
        click.echo(f"{name}: stored ({len(value)} chars, prefix={value[:4]}***)")
    else:
        click.echo(f"{name}: not stored")
        raise SystemExit(1)
