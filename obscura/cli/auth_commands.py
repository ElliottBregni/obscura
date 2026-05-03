"""obscura.cli.auth_commands -- SSO login (via auth.modernized-ai.com) +
magic-link from the CLI.

Exposes three commands under the ``obscura-auth`` script entry:

* ``obscura-auth login [--provider github|google|magic]`` — SSO via
  ``auth.modernized-ai.com`` (the unified front door for all Modernized AI
  products) and a local callback server, or magic-link email.
* ``obscura-auth logout`` — delete stored credentials.
* ``obscura-auth whoami`` — print the currently authenticated user.

OAuth flow: CLI opens ``auth.modernized-ai.com/?next=<localhost>&response_mode=query``.
The auth site does the OAuth dance with Supabase, then redirects back to the
local callback with tokens in the query string. CLI parses tokens, persists
the session, syncs provider secrets to Supabase user_metadata.

Refresh + magic-link still call Supabase REST endpoints directly:

* ``/auth/v1/token?grant_type=refresh_token`` — session refresh.
* ``/auth/v1/otp`` + ``/auth/v1/verify`` — magic link.

Override the SSO host with ``OBSCURA_SSO_AUTH_HOST`` for testing against a
local copy of the auth site.
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import shlex
import socket
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Any, cast

from typing import override

import click
import httpx

from obscura.auth import profile as _profile
from obscura.auth import secrets as _secrets
from obscura.auth import supabase_secrets as _vault
from obscura.auth.cli_session import (
    CREDENTIALS_PATH,
    StoredSession,
    SupabaseCliConfig,
    _decode_jwt_payload,
    _sync_provider_secrets_to_supabase,
    clear_session,
    get_access_token,
    get_github_token,
    load_session,
    save_session,
)

_SSO_AUTH_HOST = os.environ.get(
    "OBSCURA_SSO_AUTH_HOST",
    "https://auth.modernized-ai.com",
).rstrip("/")

logger = logging.getLogger(__name__)


def ensure_github_oauth_session(*, open_browser: bool = True) -> StoredSession | None:
    """Ensure a valid GitHub OAuth session exists for CLI startup.

    Returns the current or newly-created session when Supabase is configured.
    Returns ``None`` when Supabase is not configured.
    """
    cfg = SupabaseCliConfig.from_env()
    if cfg is None:
        return None

    token = get_access_token()
    if token:
        existing = load_session()
        if existing is not None:
            return existing

    session = _run_oauth_flow(cfg, "github", open_browser=open_browser)
    click.secho(f"Signed in as {session.email or session.user_id}.", fg="green")
    click.echo(f"Credentials stored at {CREDENTIALS_PATH}")
    return session


# ---------------------------------------------------------------------------
# Local callback server
# ---------------------------------------------------------------------------


_CLI_DONE_URL = f"{_SSO_AUTH_HOST}/cli-done/"

_CALLBACK_HTML_ERROR = """<!doctype html>
<html><head><title>Obscura — sign-in failed</title></head>
<body style="font-family:system-ui;background:#111;color:#eee;padding:2rem">
<h1>Sign-in failed</h1><pre>{error}</pre></body></html>
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class _CallbackResult:
    tokens: dict[str, str] | None = None
    error: str | None = None


def _build_callback_handler(
    result: _CallbackResult,
    done: threading.Event,
) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        @override
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)

            err = qs.get("error_description", qs.get("error", [None]))[0]
            access_token = qs.get("access_token", [None])[0]
            refresh_token = qs.get("refresh_token", [None])[0]

            if err:
                result.error = err
                body = _CALLBACK_HTML_ERROR.format(error=err).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                done.set()
                return

            if access_token and refresh_token:
                result.tokens = {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_in": qs.get("expires_in", ["3600"])[0],
                    "provider_token": qs.get("provider_token", [""])[0],
                }
                # Redirect to the hosted "you're done" page so the address bar
                # doesn't keep showing tokens.
                self.send_response(302)
                self.send_header("Location", _CLI_DONE_URL)
                self.send_header("Content-Length", "0")
                self.end_headers()
                done.set()
                return

            self.send_response(404)
            self.end_headers()

    return Handler


def _run_oauth_flow(
    cfg: SupabaseCliConfig,
    provider: str,
    *,
    timeout_seconds: float = 300.0,
    open_browser: bool = True,
) -> StoredSession:
    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    sso_url = f"{_SSO_AUTH_HOST}/?" + urllib.parse.urlencode(
        {
            "next": redirect_uri,
            "response_mode": "query",
            "provider": provider,
        },
    )

    result = _CallbackResult()
    done = threading.Event()
    handler = _build_callback_handler(result, done)
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        click.echo(f"Opening browser to sign in with {provider} via {_SSO_AUTH_HOST}…")
        click.echo(f"If it didn't open, visit: {sso_url}")
        if open_browser:
            webbrowser.open(sso_url)

        if not done.wait(timeout=timeout_seconds):
            raise RuntimeError("Timed out waiting for SSO callback")
    finally:
        server.shutdown()
        server.server_close()

    if result.error:
        raise RuntimeError(f"Sign-in failed: {result.error}")
    if not result.tokens:
        raise RuntimeError("No tokens received from SSO callback")

    tokens = result.tokens
    claims = _decode_jwt_payload(tokens["access_token"])
    session = StoredSession(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        expires_at=int(time.time()) + int(tokens["expires_in"]),
        user_id=str(claims.get("sub", "")),
        email=str(claims.get("email", "")),
        provider=provider,
        provider_token=tokens["provider_token"] or None,
        provider_refresh_token=None,
    )
    save_session(session)
    _sync_provider_secrets_to_supabase(cfg, provider=provider, session=session)
    return session


def _send_magic_link(cfg: SupabaseCliConfig, email: str) -> None:
    resp = httpx.post(
        f"{cfg.url}/auth/v1/otp",
        headers={"apikey": cfg.anon_key, "Content-Type": "application/json"},
        json={"email": email, "create_user": True},
        timeout=20.0,
    )
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"OTP request failed ({resp.status_code}): {resp.text}")


def _verify_otp(cfg: SupabaseCliConfig, email: str, token: str) -> StoredSession:
    resp = httpx.post(
        f"{cfg.url}/auth/v1/verify",
        headers={"apikey": cfg.anon_key, "Content-Type": "application/json"},
        json={"type": "email", "email": email, "token": token},
        timeout=20.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OTP verify failed ({resp.status_code}): {resp.text}")
    body: dict[str, Any] = cast(dict[str, Any], resp.json())
    user_raw: Any = body.get("user") or {}
    user: dict[str, Any] = (
        cast(dict[str, Any], user_raw) if isinstance(user_raw, dict) else {}
    )
    session = StoredSession(
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        expires_at=int(time.time()) + int(body.get("expires_in", 3600)),
        user_id=str(user.get("id", "")),
        email=str(user.get("email", email)),
        provider="magic",
    )
    save_session(session)
    return session


# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------


@click.group("auth")
def auth_group() -> None:
    """Supabase OAuth login for the Obscura CLI."""


@auth_group.command("login")
@click.option(
    "--provider",
    type=click.Choice(["github", "google", "magic"], case_sensitive=False),
    default="github",
    help="OAuth provider, or 'magic' for email magic-link.",
)
@click.option("--email", default=None, help="Email (required for --provider magic).")
@click.option("--no-browser", is_flag=True, help="Don't auto-open the browser.")
def login(provider: str, email: str | None, no_browser: bool) -> None:
    """Sign in via Supabase OAuth or magic-link email."""
    cfg = SupabaseCliConfig.from_env()
    if cfg is None:
        raise click.ClickException(
            "Supabase is not configured. Set SUPABASE_URL and SUPABASE_ANON_KEY.",
        )

    provider = provider.lower()

    if provider == "magic":
        if not email:
            email = click.prompt("Email")
        assert email is not None
        try:
            _send_magic_link(cfg, email)
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Magic-link email sent to {email}.")
        token = click.prompt("Paste the 6-digit code from the email", hide_input=False)
        try:
            session = _verify_otp(cfg, email, str(token).strip())
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc
    else:
        try:
            session = _run_oauth_flow(cfg, provider, open_browser=not no_browser)
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc

    click.secho(f"Signed in as {session.email or session.user_id}.", fg="green")
    click.echo(f"Credentials stored at {CREDENTIALS_PATH}")


@auth_group.command("logout")
def logout() -> None:
    """Remove stored Supabase credentials from this machine."""
    if clear_session():
        click.echo(f"Removed {CREDENTIALS_PATH}.")
    else:
        click.echo("No stored credentials to remove.")


@auth_group.command("whoami")
def whoami() -> None:
    """Print the currently authenticated Supabase user."""
    session = load_session()
    if session is None:
        click.echo("Not signed in.")
        raise SystemExit(1)

    remaining = session.expires_at - int(time.time())
    state = "valid" if remaining > 0 else "EXPIRED"
    gh_state = "yes" if session.provider_token else "no"
    click.echo(f"user:        {session.email or session.user_id}")
    click.echo(f"user_id:     {session.user_id}")
    click.echo(f"provider:    {session.provider}")
    click.echo(f"token:       {state} (expires in {max(0, remaining)}s)")
    click.echo(f"github oauth: {gh_state}")
    click.echo(f"file:        {CREDENTIALS_PATH}")


# ---------------------------------------------------------------------------
# `obscura-auth secrets` — store Supabase config in the OS keyring
# ---------------------------------------------------------------------------


@auth_group.group("secrets")
def secrets_group() -> None:
    """Store Supabase config in the OS keyring (Keychain / Credential Manager).

    The resolver always prefers env vars, so anything set here is overridden
    by ``SUPABASE_*`` env vars at runtime -- safe for Docker/CI which keep
    passing secrets via ``-e``.
    """


def _validate_secret_name(name: str, *, force: bool = False) -> str:
    """Normalise *name* and optionally reject unknown identifiers.

    The allowlist catches typos on common keys (``SUPABSE_URL`` instead of
    ``SUPABASE_URL``). Pass ``force=True`` to store an arbitrary name --
    useful for one-off API keys that aren't in the built-in catalog.
    """
    normalized = name.strip().upper()
    if not force and normalized not in _secrets.KNOWN_SECRET_NAMES:
        known = ", ".join(_secrets.KNOWN_SECRET_NAMES)
        raise click.ClickException(
            f"Unknown secret '{name}'. Pass --force to store an arbitrary name, "
            f"or pick from: {known}",
        )
    return normalized


@secrets_group.command("set")
@click.argument("name")
@click.option(
    "--value",
    default=None,
    help="Secret value. Omit to be prompted with hidden input.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Accept an arbitrary NAME outside the built-in catalog.",
)
def secrets_set(name: str, value: str | None, force: bool) -> None:
    """Store a service secret in the OS keyring.

    Known names cover Supabase identity config, LLM backend keys
    (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, ``GITHUB_TOKEN``, …), and
    common plugin credentials (``NOTION_TOKEN``, ``QDRANT_API_KEY``, …).
    Use ``--force`` for anything else.
    """
    normalized = _validate_secret_name(name, force=force)
    if not _secrets.keyring_available():
        raise click.ClickException(
            "No OS keyring backend available on this system. "
            f"Set {normalized} as an env var or in ~/.obscura/.env instead.",
        )
    if value is None:
        hide = normalized in _secrets.SENSITIVE_SECRET_NAMES
        value = click.prompt(f"Value for {normalized}", hide_input=hide)
    try:
        stored = _secrets.store(normalized, str(value))
    except _secrets.SecretsValidationError as exc:
        raise click.ClickException(str(exc)) from exc
    if not stored:
        raise click.ClickException(f"Failed to store {normalized} in keyring.")
    click.secho(f"Stored {normalized} in keyring.", fg="green")


@secrets_group.command("get")
@click.argument("name")
@click.option("--reveal", is_flag=True, help="Print the full value (off by default).")
@click.option(
    "--force",
    is_flag=True,
    help="Accept an arbitrary NAME outside the built-in catalog.",
)
def secrets_get(name: str, reveal: bool, force: bool) -> None:
    """Show where a stored secret is resolved from."""
    normalized = _validate_secret_name(name, force=force)
    value = _secrets.resolve(normalized)
    if value is None:
        click.echo(f"{normalized}: (unset)")
        raise SystemExit(1)
    source = _secrets.sources([normalized]).get(normalized, "missing")
    shown = value if reveal else _secrets.mask(value)
    click.echo(f"{normalized}: {shown} [source: {source}]")


@secrets_group.command("delete")
@click.argument("name")
@click.option(
    "--force",
    is_flag=True,
    help="Accept an arbitrary NAME outside the built-in catalog.",
)
def secrets_delete(name: str, force: bool) -> None:
    """Remove a stored secret from the OS keyring."""
    normalized = _validate_secret_name(name, force=force)
    if _secrets.delete(normalized):
        click.secho(f"Removed {normalized} from keyring.", fg="green")
    else:
        click.echo(f"No keyring entry found for {normalized}.")


@secrets_group.command("list")
@click.option(
    "--only-set",
    is_flag=True,
    help="Skip names that aren't configured anywhere.",
)
def secrets_list(only_set: bool) -> None:
    """Show where every known secret is currently resolved from."""
    mapping = _secrets.sources()
    kr_ready = _secrets.keyring_available()
    click.echo(f"Keyring backend: {'available' if kr_ready else 'unavailable'}")
    width = max(len(name) for name in mapping)
    for name, source in mapping.items():
        if only_set and source == "missing":
            continue
        click.echo(f"  {name.ljust(width)}  {source}")


@secrets_group.command("export")
@click.option(
    "--shell",
    type=click.Choice(["bash", "fish"], case_sensitive=False),
    default="bash",
    help="Output syntax. Defaults to POSIX (bash/zsh).",
)
@click.option(
    "--only",
    default=None,
    help="Comma-separated subset of names to export (default: all configured).",
)
def secrets_export(shell: str, only: str | None) -> None:
    """Print shell ``export`` lines for every configured secret.

    Designed for one-liners::

        eval "$(obscura-auth secrets export)"
        obscura-auth secrets export --shell fish | source

    Values are shell-escaped so paste-in-random-string secrets don't break
    the shell. Commented lines are NEVER emitted for unset names.
    """
    if only:
        requested = [n.strip().upper() for n in only.split(",") if n.strip()]
    else:
        requested = list(_secrets.KNOWN_SECRET_NAMES)

    for name in requested:
        value = _secrets.resolve(name)
        if value is None:
            continue
        quoted = shlex.quote(value)
        if shell.lower() == "fish":
            click.echo(f"set -gx {name} {quoted}")
        else:
            click.echo(f"export {name}={quoted}")


@secrets_group.command("strict-env")
@click.option(
    "--tail",
    type=int,
    default=20,
    help="How many recent audit entries to show (default: 20).",
)
@click.option(
    "--clear",
    is_flag=True,
    help="Truncate the audit log after showing status.",
)
def secrets_strict_env(tail: int, clear: bool) -> None:
    """Show strict-env status and recent audit entries.

    Strict env mode (``OBSCURA_TOOL_ENV_STRICT=1``) strips known secret
    names from the process env before Obscura spawns subprocesses. Every
    strip event lands in a JSONL audit log so you can see which tool
    was shielded from which keys, after the fact.

    Enable it for a session::

        export OBSCURA_TOOL_ENV_STRICT=1
        obscura

    Or persist it for your user by adding the line to ``~/.obscura/.env``.
    """
    strict_on = os.environ.get("OBSCURA_TOOL_ENV_STRICT", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    log_path = _secrets.audit_log_path()

    click.echo(f"Strict mode: {'ON' if strict_on else 'off'}")
    if not strict_on:
        click.echo(
            "  Enable with: export OBSCURA_TOOL_ENV_STRICT=1 "
            "(or add to ~/.obscura/.env)",
        )
    click.echo(f"Audit log:   {log_path}")

    if clear:
        if log_path.exists():
            log_path.unlink()
            click.secho(f"Cleared {log_path}.", fg="green")
        else:
            click.echo("Audit log doesn't exist yet; nothing to clear.")
        return

    if not log_path.exists():
        click.echo("No audit entries yet.")
        return

    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail_lines = lines[-tail:] if tail > 0 else lines
    click.echo(f"Recent entries ({len(tail_lines)} of {len(lines)}):")
    for raw in tail_lines:
        if not raw.strip():
            continue
        try:
            entry: dict[str, Any] = cast(dict[str, Any], json.loads(raw))
        except ValueError:
            logger.debug("suppressed exception in secrets_strict_env", exc_info=True)
            click.echo(f"  [malformed] {raw}")
            continue
        ts = entry.get("ts", "?")
        stripped: Any = entry.get("stripped") or []
        if isinstance(stripped, list):
            names = ", ".join(str(n) for n in cast(list[Any], stripped))
        else:
            names = str(stripped)
        click.echo(f"  {ts}  stripped={names}")


# ---------------------------------------------------------------------------
# `obscura-auth secrets cloud` -- Supabase user_metadata vault
# ---------------------------------------------------------------------------
#
# Design notes:
#
# * Values are Fernet-encrypted with a key derived from the user's
#   email + a per-user salt. No passphrase in this cut -- see the
#   module docstring for the threat model. A future passphrase-based
#   tier will layer on top, scoped to keys the user marks ``--risk``.
# * ``_vault_client()`` returns ``None`` when Supabase isn't configured
#   so the commands can print a specific "sign in first" error instead
#   of a vague failure.


def _vault_client() -> Any:
    """Return the Supabase vault client or raise a clean Click error."""
    client = _vault.get_client()
    if client is None:
        raise click.ClickException(
            "Supabase is not configured. Set SUPABASE_URL and "
            "SUPABASE_ANON_KEY, or run `obscura-auth login`.",
        )
    return client


@secrets_group.group("cloud")
def secrets_cloud_group() -> None:
    """Per-user encrypted cloud vault in Supabase ``user_metadata``.

    Values are Fernet-encrypted with a key derived from your account
    email. The primary flows are ``pull-all`` on a fresh machine and
    ``push NAME`` when you want to sync a local keyring entry up.
    """


def _prompt_passphrase(
    prompt_text: str = "Passphrase",
    *,
    confirm: bool = False,
) -> str:
    """Prompt for a passphrase with hidden input."""
    while True:
        value = click.prompt(prompt_text, hide_input=True)
        if not isinstance(value, str) or not value:
            click.secho("Passphrase cannot be empty.", fg="red")
            continue
        if confirm:
            again = click.prompt("Confirm passphrase", hide_input=True)
            if value != again:
                click.secho("Passphrases don't match. Try again.", fg="red")
                continue
        return value


@secrets_cloud_group.command("status")
def secrets_cloud_status() -> None:
    """List names stored in the cloud vaults (regular + risky)."""
    client = _vault_client()
    try:
        names = client.names()
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc

    if not names:
        click.echo("No entries in the cloud vault.")
        return
    click.echo(f"Entries ({len(names)}):")
    for name, is_risky in names:
        marker = " [risk]" if is_risky else ""
        click.echo(f"  {name}{marker}")
    if any(r for _, r in names):
        click.echo(
            "\n[risk]-tagged entries need a passphrase to decrypt. "
            "Use `cloud passphrase set` to enter it.",
        )


@secrets_cloud_group.group("passphrase")
def secrets_cloud_passphrase_group() -> None:
    """Manage the passphrase used to encrypt ``--risk`` entries."""


@secrets_cloud_passphrase_group.command("set")
def secrets_cloud_passphrase_set() -> None:
    """Prompt for a passphrase and cache the derived key locally.

    Re-run this after rotating the passphrase or on a fresh machine.
    The passphrase itself is never stored; only a scrypt-derived Fernet
    key lives in the OS keyring.
    """
    client = _vault_client()
    passphrase = _prompt_passphrase(
        "Cloud risky-vault passphrase",
        confirm=True,
    )
    try:
        client.set_passphrase(passphrase)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    click.secho("Passphrase accepted and cached in the OS keyring.", fg="green")


@secrets_cloud_passphrase_group.command("clear")
def secrets_cloud_passphrase_clear() -> None:
    """Forget the cached passphrase key on this machine."""
    client = _vault.get_client()
    if client is not None:
        client.clear_passphrase()
    else:
        _vault.clear_passphrase_key()
    click.secho("Passphrase key cleared from this machine.", fg="green")


@secrets_cloud_group.command("pull")
@click.argument("name")
@click.option(
    "--to-keyring/--print",
    "to_keyring",
    default=True,
    help="Write into the OS keyring (default) or print to stdout.",
)
def secrets_cloud_pull(name: str, to_keyring: bool) -> None:
    """Fetch a secret from the cloud vault.

    If the name lives in the ``--risk`` vault, you'll be prompted for
    the passphrase on first use. Subsequent pulls reuse the cached key.
    """
    client = _vault_client()
    normalized = name.strip().upper()

    try:
        value = client.get(normalized)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc

    # If nothing came back but the name is in the risky vault, prompt
    # and retry. ``names()`` reveals inventory without needing a key.
    if value is None:
        try:
            inventory = dict(client.names())
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(str(exc)) from exc
        if inventory.get(normalized):
            click.echo(
                f"{normalized} is in the --risk vault and needs a "
                "passphrase to decrypt.",
            )
            passphrase = _prompt_passphrase("Cloud risky-vault passphrase")
            try:
                client.set_passphrase(passphrase)
                value = client.get(normalized)
            except Exception as exc:  # noqa: BLE001
                raise click.ClickException(str(exc)) from exc

    if value is None:
        raise click.ClickException(f"{normalized} not found in the cloud vault.")

    if to_keyring:
        if not _secrets.keyring_available():
            raise click.ClickException(
                "No OS keyring backend available. Use --print instead.",
            )
        try:
            stored = _secrets.store(normalized, value)
        except _secrets.SecretsValidationError as exc:
            raise click.ClickException(str(exc)) from exc
        if not stored:
            raise click.ClickException(
                f"Failed to write {normalized} to the keyring.",
            )
        click.secho(f"Pulled {normalized} into the OS keyring.", fg="green")
    else:
        click.echo(value)


@secrets_cloud_group.command("push")
@click.argument("name")
@click.option(
    "--value",
    default=None,
    help="Explicit value. Omit to read from the OS keyring.",
)
@click.option(
    "--risk",
    is_flag=True,
    help="Route to the passphrase-protected 'risky' vault.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Skip the interactive Y/N confirmation.",
)
def secrets_cloud_push(
    name: str,
    value: str | None,
    risk: bool,
    yes: bool,
) -> None:
    """Encrypt and push a secret to the cloud vault.

    By default goes to the regular (email-key encrypted) vault. Pass
    ``--risk`` to route to the passphrase-protected vault; you'll be
    prompted for the passphrase if no key is cached yet.

    Reads the value from the OS keyring by default -- typical flow is
    ``secrets set`` locally, then ``cloud push`` to sync. ``--value``
    overrides. Always confirms interactively unless ``--yes`` is passed.
    """
    client = _vault_client()
    normalized = name.strip().upper()

    if value is None:
        resolved = _secrets.resolve(normalized)
        if resolved is None:
            raise click.ClickException(
                f"{normalized} is not in your local keyring/env. Use "
                f"`obscura-auth secrets set {normalized}` first, or pass "
                "--value.",
            )
        value = resolved

    preview = _secrets.mask(value)
    tag = " (risky vault)" if risk else ""
    click.echo(
        f"About to push {normalized} = {preview} (encrypted){tag} "
        "to your Supabase cloud vault.",
    )
    if not yes and not click.confirm("Continue?", default=False):
        click.echo("Aborted.")
        return

    if risk and not client.has_passphrase_key():
        click.echo(
            "The --risk vault requires a passphrase. You'll only be "
            "asked for it once per machine; the derived key is cached "
            "in the OS keyring.",
        )
        passphrase = _prompt_passphrase(
            "Cloud risky-vault passphrase",
            confirm=True,
        )
        try:
            client.set_passphrase(passphrase)
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(str(exc)) from exc

    try:
        client.push(normalized, value, risk=risk)
    except _vault.VaultPushBlocked as exc:
        raise click.ClickException(str(exc)) from exc
    except _vault.PassphraseRequired as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    click.secho(f"Pushed {normalized} to the cloud vault.", fg="green")


@secrets_cloud_group.command("delete")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
def secrets_cloud_delete(name: str, yes: bool) -> None:
    """Remove a secret from the cloud vault."""
    client = _vault_client()
    normalized = name.strip().upper()

    if not yes and not click.confirm(
        f"Delete {normalized} from the cloud vault?",
        default=False,
    ):
        click.echo("Aborted.")
        return

    try:
        removed = client.delete(normalized)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    if removed:
        click.secho(f"Removed {normalized} from the cloud vault.", fg="green")
    else:
        click.echo(f"{normalized} wasn't in the cloud vault.")


@secrets_cloud_group.command("pull-all")
def secrets_cloud_pull_all() -> None:
    """Pull every cloud entry into the OS keyring.

    Convenient for fresh-machine setup. Prompts once for the risky-vault
    passphrase up front when the vault has risky entries that aren't
    already unlocked on this machine.
    """
    client = _vault_client()

    if not _secrets.keyring_available():
        raise click.ClickException(
            "No OS keyring backend available on this machine.",
        )

    # Prompt for the passphrase up front when needed, so the snapshot
    # call below can decrypt everything in one pass.
    try:
        needs_passphrase = (
            client.has_risky_entries() and not client.has_passphrase_key()
        )
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    if needs_passphrase:
        click.echo(
            "The vault has --risk entries that need a passphrase to decrypt.",
        )
        passphrase = _prompt_passphrase("Cloud risky-vault passphrase")
        try:
            client.set_passphrase(passphrase)
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(str(exc)) from exc

    try:
        snapshot = client.snapshot()
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc

    if not snapshot:
        click.echo("Nothing to pull -- the cloud vault is empty.")
        return

    pulled: list[str] = []
    for candidate, value in snapshot.items():
        try:
            ok = _secrets.store(candidate, value)
        except _secrets.SecretsValidationError as exc:
            logger.debug(
                "suppressed exception in secrets_cloud_pull_all", exc_info=True
            )
            click.secho(f"  skipped {candidate}: {exc}", fg="yellow")
            continue
        if ok:
            pulled.append(candidate)
    click.secho(
        f"Pulled {len(pulled)} of {len(snapshot)} entries into the keyring.",
        fg="green",
    )


# ---------------------------------------------------------------------------
# `obscura-auth profile` -- Supabase user_metadata.obscura_profile
# ---------------------------------------------------------------------------
#
# Companion to the cloud vault: non-secret preferences, backend
# defaults, feature flags, and the list of machines the user has
# signed in on. Reads/writes to the same ``user_metadata`` row via
# the user's session JWT -- no passphrase, no encryption.


def _profile_client() -> Any:
    client = _profile.get_client()
    if client is None:
        raise click.ClickException(
            "Supabase is not configured. Set SUPABASE_URL and "
            "SUPABASE_ANON_KEY, or run `obscura-auth login`.",
        )
    return client


# Field-name → Python type coercer for ``profile set``. Scalars coerce
# from the raw CLI string; list fields split on commas.
_PROFILE_FIELD_TYPES: dict[str, str] = {
    "display_name": "str",
    "timezone": "str",
    "default_backend": "str",
    "default_model": "str",
    "undercover": "bool",
    "feature_flags": "list",
    "last_workspace": "str",
    "last_session_id": "str",
    "last_cwd": "str",
}


def _coerce_profile_value(field_name: str, raw: str) -> Any:
    kind = _PROFILE_FIELD_TYPES.get(field_name)
    if kind is None:
        raise click.ClickException(
            f"Unknown profile field '{field_name}'. Known: "
            + ", ".join(sorted(_PROFILE_FIELD_TYPES)),
        )
    if kind == "bool":
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise click.ClickException(
            f"Expected a boolean for '{field_name}' (got {raw!r}).",
        )
    if kind == "list":
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


@auth_group.group("profile")
def profile_group() -> None:
    """Read / write the user's Supabase ``user_metadata.obscura_profile``.

    Plaintext, non-secret preferences and the list of machines you've
    registered. Secrets belong in ``obscura-auth secrets cloud`` (which
    writes to a sibling encrypted key in the same row).
    """


@profile_group.command("show")
def profile_show() -> None:
    """Print the full profile in human-readable form."""
    client = _profile_client()
    try:
        profile = client.load()
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc

    data = profile.model_dump(mode="json")
    devices = data.pop("devices", [])
    click.echo(json.dumps(data, indent=2, sort_keys=True))
    if devices:
        click.echo(f"\nDevices ({len(devices)}):")
        for dev in devices:
            click.echo(f"  - {dev['name']} ({dev['id']})")
            click.echo(
                f"      platform={dev['platform']} "
                f"host={dev['hostname']} "
                f"last_seen={dev['last_seen']}",
            )
    else:
        click.echo("\nDevices: none registered")


@profile_group.command("get")
@click.argument("field_name")
def profile_get(field_name: str) -> None:
    """Print a single field's value."""
    if field_name == "devices":
        raise click.ClickException(
            "Use `profile device list` to view devices.",
        )
    if field_name not in _PROFILE_FIELD_TYPES:
        raise click.ClickException(
            f"Unknown field '{field_name}'. Known: "
            + ", ".join(sorted(_PROFILE_FIELD_TYPES)),
        )

    client = _profile_client()
    try:
        profile = client.load()
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc

    value = getattr(profile, field_name)
    if value is None:
        click.echo("(unset)")
    elif isinstance(value, list):
        click.echo(json.dumps(value))
    else:
        click.echo(str(value))


@profile_group.command("set")
@click.argument("field_name")
@click.argument("value")
def profile_set(field_name: str, value: str) -> None:
    """Update one profile field.

    Lists accept comma-separated values (``feature_flags=voice,swarm``).
    Booleans accept ``true|false|yes|no|on|off|1|0``.
    """
    coerced = _coerce_profile_value(field_name, value)
    client = _profile_client()
    try:
        client.update(**{field_name: coerced})
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    click.secho(f"Updated {field_name}.", fg="green")


@profile_group.command("unset")
@click.argument("field_name")
def profile_unset(field_name: str) -> None:
    """Reset a field to its empty default (``None`` or ``[]``)."""
    if field_name == "devices":
        raise click.ClickException(
            "Use `profile device remove ID` to drop a specific device.",
        )
    if field_name not in _PROFILE_FIELD_TYPES:
        raise click.ClickException(
            f"Unknown field '{field_name}'. Known: "
            + ", ".join(sorted(_PROFILE_FIELD_TYPES)),
        )

    kind = _PROFILE_FIELD_TYPES[field_name]
    empty: Any = [] if kind == "list" else None
    client = _profile_client()
    try:
        client.update(**{field_name: empty})
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    click.secho(f"Cleared {field_name}.", fg="green")


# ---------------------------------------------------------------------------
# `obscura-auth profile device`
# ---------------------------------------------------------------------------


@profile_group.group("device")
def profile_device_group() -> None:
    """Manage the list of machines this user has registered."""


@profile_device_group.command("list")
def profile_device_list() -> None:
    """List every machine registered on the profile."""
    client = _profile_client()
    try:
        profile = client.load()
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc

    this_machine = _profile.get_or_create_machine_id()
    if not profile.devices:
        click.echo("No devices registered.")
        return
    click.echo(f"Devices ({len(profile.devices)}):")
    for dev in profile.devices:
        marker = " (this machine)" if dev.id == this_machine else ""
        click.echo(f"  {dev.name}{marker}")
        click.echo(
            f"      id={dev.id} platform={dev.platform} host={dev.hostname}",
        )
        click.echo(f"      first_seen={dev.first_seen}  last_seen={dev.last_seen}")


@profile_device_group.command("current")
def profile_device_current() -> None:
    """Print the current machine's ID + its entry on the profile (if any)."""
    machine_id = _profile.get_or_create_machine_id()
    click.echo(f"machine_id: {machine_id}")

    client = _profile_client()
    try:
        profile = client.load()
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc

    matching = next((d for d in profile.devices if d.id == machine_id), None)
    if matching is None:
        click.echo("This machine isn't registered yet.")
        click.echo(
            "Run `obscura-auth profile device register` to add it.",
        )
        return
    click.echo(f"name:       {matching.name}")
    click.echo(f"platform:   {matching.platform}")
    click.echo(f"hostname:   {matching.hostname}")
    click.echo(f"first_seen: {matching.first_seen}")
    click.echo(f"last_seen:  {matching.last_seen}")


@profile_device_group.command("register")
@click.option(
    "--name",
    default=None,
    help="Human-readable label for this machine (defaults to hostname).",
)
def profile_device_register(name: str | None) -> None:
    """Add (or refresh) this machine's entry on the profile."""
    client = _profile_client()
    try:
        entry = client.register_device(name)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    click.secho(
        f"Registered '{entry.name}' (id={entry.id}) on your profile.",
        fg="green",
    )


@profile_device_group.command("rename")
@click.argument("new_name")
def profile_device_rename(new_name: str) -> None:
    """Rename the current machine's entry."""
    client = _profile_client()
    try:
        entry = client.rename_device(new_name)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    click.secho(f"Renamed to '{entry.name}'.", fg="green")


@profile_device_group.command("remove")
@click.argument("device_id")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
def profile_device_remove(device_id: str, yes: bool) -> None:
    """Drop a device from the profile (e.g. a wiped or lost machine)."""
    if not yes and not click.confirm(
        f"Remove device {device_id} from your profile?",
        default=False,
    ):
        click.echo("Aborted.")
        return

    client = _profile_client()
    try:
        removed = client.remove_device(device_id)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    if removed:
        click.secho(f"Removed device {device_id}.", fg="green")
    else:
        click.echo(f"No device with id {device_id} on your profile.")


@profile_device_group.command("touch")
def profile_device_touch() -> None:
    """Bump this machine's ``last_seen`` timestamp on the profile."""
    client = _profile_client()
    try:
        entry = client.touch_device()
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    if entry is None:
        click.echo(
            "This machine isn't registered yet. Run "
            "`obscura-auth profile device register` first.",
        )
        return
    click.secho(f"Updated last_seen for '{entry.name}'.", fg="green")


__all__ = [
    "CREDENTIALS_PATH",
    "StoredSession",
    "SupabaseCliConfig",
    "auth_group",
    "clear_session",
    "get_access_token",
    "get_github_token",
    "ensure_github_oauth_session",
    "load_session",
    "save_session",
]
