"""obscura.cli.auth_commands -- Supabase OAuth + magic-link login from the CLI.

Exposes three commands under ``obscura auth``:

* ``obscura auth login [--provider github|google|magic]`` — OAuth via a local
  callback server, or magic-link email. Persists the resulting session to
  ``~/.obscura/credentials.json``.
* ``obscura auth logout`` — delete the stored credentials (local only).
* ``obscura auth whoami`` — print the currently authenticated user.

The flow deliberately avoids adding Supabase-specific Python deps. It uses
Supabase's REST auth endpoints directly:

* ``/auth/v1/authorize`` — OAuth redirect with PKCE.
* ``/auth/v1/token?grant_type=pkce`` — code → session exchange.
* ``/auth/v1/token?grant_type=refresh_token`` — session refresh.
* ``/auth/v1/otp`` — magic link.
* ``/auth/v1/user`` — identity probe.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import httpx

logger = logging.getLogger(__name__)

CREDENTIALS_PATH = Path(
    os.environ.get("OBSCURA_CREDENTIALS_FILE")
    or (Path(os.environ.get("OBSCURA_HOME", Path.home() / ".obscura")) / "credentials.json"),
)

# Refresh the access token this many seconds before it actually expires so
# callers never see a just-expired token.
REFRESH_LEEWAY_SECONDS = 60


@dataclass(frozen=True)
class SupabaseCliConfig:
    """Runtime config resolved from env vars."""

    url: str
    anon_key: str

    @classmethod
    def from_env(cls) -> SupabaseCliConfig | None:
        url = os.environ.get("SUPABASE_URL", "").rstrip("/")
        anon = os.environ.get("SUPABASE_ANON_KEY", "")
        if not url or not anon:
            return None
        return cls(url=url, anon_key=anon)


@dataclass
class StoredSession:
    """Shape of the JSON file persisted at ``CREDENTIALS_PATH``."""

    access_token: str
    refresh_token: str
    expires_at: int  # unix seconds
    user_id: str
    email: str
    provider: str  # "github" | "google" | "magic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "user_id": self.user_id,
            "email": self.email,
            "provider": self.provider,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StoredSession:
        return cls(
            access_token=str(d["access_token"]),
            refresh_token=str(d["refresh_token"]),
            expires_at=int(d["expires_at"]),
            user_id=str(d.get("user_id", "")),
            email=str(d.get("email", "")),
            provider=str(d.get("provider", "")),
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_session(session: StoredSession) -> None:
    """Write *session* to ``CREDENTIALS_PATH`` with 0600 perms."""
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(session.to_dict(), indent=2)
    CREDENTIALS_PATH.write_text(data)
    try:
        CREDENTIALS_PATH.chmod(0o600)
    except OSError:
        pass


def load_session() -> StoredSession | None:
    """Load a stored session, or ``None`` if the file is missing/corrupt."""
    if not CREDENTIALS_PATH.exists():
        return None
    try:
        raw = json.loads(CREDENTIALS_PATH.read_text())
        return StoredSession.from_dict(raw)
    except (OSError, ValueError, KeyError):
        return None


def clear_session() -> bool:
    """Delete the credentials file. Returns True when a file was removed."""
    if CREDENTIALS_PATH.exists():
        CREDENTIALS_PATH.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Session refresh + access-token accessor
# ---------------------------------------------------------------------------


def _refresh_session(
    cfg: SupabaseCliConfig,
    refresh_token: str,
) -> StoredSession:
    """Exchange a refresh token for a fresh session."""
    resp = httpx.post(
        f"{cfg.url}/auth/v1/token",
        params={"grant_type": "refresh_token"},
        headers={
            "apikey": cfg.anon_key,
            "Content-Type": "application/json",
        },
        json={"refresh_token": refresh_token},
        timeout=20.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"refresh failed ({resp.status_code}): {resp.text}")
    body = resp.json()
    user = body.get("user") or {}
    return StoredSession(
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        expires_at=int(time.time()) + int(body.get("expires_in", 3600)),
        user_id=str(user.get("id", "")),
        email=str(user.get("email", "")),
        provider="refresh",
    )


def get_access_token() -> str | None:
    """Return a valid access token, refreshing if needed.

    This is the entry point used by non-CLI callers (e.g. API clients
    that want to authenticate to an Obscura server as the current user).
    Returns ``None`` when no session is stored or Supabase isn't configured.
    """
    session = load_session()
    if session is None:
        return None

    if session.expires_at - REFRESH_LEEWAY_SECONDS > int(time.time()):
        return session.access_token

    cfg = SupabaseCliConfig.from_env()
    if cfg is None:
        # Can't refresh — but the token may still be valid for a moment.
        return session.access_token if session.expires_at > int(time.time()) else None

    try:
        refreshed = _refresh_session(cfg, session.refresh_token)
    except Exception as exc:  # network / auth error
        logger.debug("CLI token refresh failed: %s", exc)
        return None
    # Preserve the original provider label for display purposes.
    refreshed.provider = session.provider
    save_session(refreshed)
    return refreshed.access_token


# ---------------------------------------------------------------------------
# Local callback server for OAuth redirect
# ---------------------------------------------------------------------------


_CALLBACK_HTML_SUCCESS = """<!doctype html>
<html><head><title>Obscura — signed in</title>
<style>body{font-family:system-ui;background:#111;color:#eee;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.card{background:#1b1b1b;padding:2rem 3rem;border-radius:12px;border:1px solid #333;max-width:420px;text-align:center}
h1{margin:0 0 .5rem;font-size:1.25rem}p{margin:0;color:#aaa;font-size:.9rem}</style></head>
<body><div class="card"><h1>Signed in to Obscura</h1>
<p>You can close this window and return to the terminal.</p></div></body></html>
"""

_CALLBACK_HTML_ERROR = """<!doctype html>
<html><head><title>Obscura — sign-in failed</title></head>
<body style="font-family:system-ui;background:#111;color:#eee;padding:2rem">
<h1>Sign-in failed</h1><pre>{error}</pre></body></html>
"""


def _pkce_pair() -> tuple[str, str]:
    """Generate a ``(code_verifier, code_challenge)`` PKCE pair (S256)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _free_port() -> int:
    """Pick a free loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class _CallbackResult:
    code: str | None = None
    error: str | None = None


def _build_callback_handler(
    result: _CallbackResult,
    done: threading.Event,
) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        # Silence the default stderr logging.
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)

            if "error" in qs:
                result.error = qs["error"][0]
                body = _CALLBACK_HTML_ERROR.format(error=result.error).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                done.set()
                return

            code = qs.get("code", [None])[0]
            if code:
                result.code = code
                body = _CALLBACK_HTML_SUCCESS.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
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
    """Drive the full PKCE OAuth handshake and return a persisted session."""
    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    verifier, challenge = _pkce_pair()

    authorize_url = (
        f"{cfg.url}/auth/v1/authorize?"
        + urllib.parse.urlencode(
            {
                "provider": provider,
                "redirect_to": redirect_uri,
                "code_challenge": challenge,
                "code_challenge_method": "s256",
            },
        )
    )

    result = _CallbackResult()
    done = threading.Event()
    handler = _build_callback_handler(result, done)
    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        click.echo(f"Opening browser to sign in with {provider}…")
        click.echo(f"If it didn't open, visit: {authorize_url}")
        if open_browser:
            webbrowser.open(authorize_url)

        if not done.wait(timeout=timeout_seconds):
            raise RuntimeError("Timed out waiting for OAuth callback")
    finally:
        server.shutdown()
        server.server_close()

    if result.error:
        raise RuntimeError(f"OAuth provider returned error: {result.error}")
    if not result.code:
        raise RuntimeError("No authorization code received")

    # Exchange code for session
    resp = httpx.post(
        f"{cfg.url}/auth/v1/token",
        params={"grant_type": "pkce"},
        headers={
            "apikey": cfg.anon_key,
            "Content-Type": "application/json",
        },
        json={"auth_code": result.code, "code_verifier": verifier},
        timeout=20.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Code exchange failed ({resp.status_code}): {resp.text}",
        )
    body = resp.json()
    user = body.get("user") or {}
    session = StoredSession(
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        expires_at=int(time.time()) + int(body.get("expires_in", 3600)),
        user_id=str(user.get("id", "")),
        email=str(user.get("email", "")),
        provider=provider,
    )
    save_session(session)
    return session


def _send_magic_link(cfg: SupabaseCliConfig, email: str) -> None:
    """Trigger an emailed OTP/magic-link for *email*."""
    resp = httpx.post(
        f"{cfg.url}/auth/v1/otp",
        headers={
            "apikey": cfg.anon_key,
            "Content-Type": "application/json",
        },
        json={"email": email, "create_user": True},
        timeout=20.0,
    )
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"OTP request failed ({resp.status_code}): {resp.text}")


def _verify_otp(
    cfg: SupabaseCliConfig,
    email: str,
    token: str,
) -> StoredSession:
    """Exchange an emailed OTP code for a session."""
    resp = httpx.post(
        f"{cfg.url}/auth/v1/verify",
        headers={
            "apikey": cfg.anon_key,
            "Content-Type": "application/json",
        },
        json={"type": "email", "email": email, "token": token},
        timeout=20.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OTP verify failed ({resp.status_code}): {resp.text}")
    body = resp.json()
    user = body.get("user") or {}
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
# Click command group
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
@click.option(
    "--email",
    default=None,
    help="Email address (required for --provider magic).",
)
@click.option(
    "--no-browser",
    is_flag=True,
    help="Don't auto-open the browser; print the URL instead.",
)
def login(provider: str, email: str | None, no_browser: bool) -> None:
    """Sign in to Obscura via Supabase OAuth or a magic-link email."""
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
    """Remove the stored Supabase credentials from this machine."""
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
    click.echo(f"user:     {session.email or session.user_id}")
    click.echo(f"user_id:  {session.user_id}")
    click.echo(f"provider: {session.provider}")
    click.echo(f"token:    {state} (expires in {max(0, remaining)}s)")
    click.echo(f"file:     {CREDENTIALS_PATH}")


__all__ = [
    "CREDENTIALS_PATH",
    "StoredSession",
    "SupabaseCliConfig",
    "auth_group",
    "clear_session",
    "get_access_token",
    "load_session",
    "save_session",
]
