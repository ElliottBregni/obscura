"""obscura.auth.cli_session -- Supabase CLI session primitives.

The auth-layer foundation for the local CLI/daemon session: config
resolution, session storage (keyring + plaintext fallback), token
refresh, and provider-secret sync. Lives below ``obscura.cli`` so that
``obscura.auth.profile`` / ``cli_user`` / ``supabase_secrets`` can read
the active session without inverting the layering.

The Click commands that drive the user-facing flows (``obscura-auth
login``/``logout``/``whoami``, the interactive OAuth callback server,
magic-link prompts) stay in :mod:`obscura.cli.auth_commands` and import
from here. Anything in this module is pure I/O + HTTP and free of Click.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import httpx

from obscura.auth import secrets as _secrets

logger = logging.getLogger(__name__)


CREDENTIALS_PATH = Path(
    os.environ.get("OBSCURA_CREDENTIALS_FILE")
    or (
        Path(os.environ.get("OBSCURA_HOME", Path.home() / ".obscura"))
        / "credentials.json"
    ),
)

REFRESH_LEEWAY_SECONDS = 60
_PROVIDER_SECRET_METADATA_KEY = "obscura_provider_secrets"

_KEYRING_SERVICE = "obscura-cli"
_KEYRING_USERNAME = "supabase-session"


@dataclass(frozen=True)
class SupabaseCliConfig:
    url: str
    anon_key: str

    @classmethod
    def from_env(cls) -> SupabaseCliConfig | None:
        """Resolve Supabase project URL + anon key.

        Uses the shared :mod:`obscura.auth.secrets` resolver so values can
        live in the process env, ``~/.obscura/.env``, or the OS keyring --
        whichever the user finds most convenient for their platform.
        """
        url = (_secrets.resolve("SUPABASE_URL") or "").rstrip("/")
        anon = _secrets.resolve("SUPABASE_ANON_KEY") or ""
        if not url or not anon:
            return None
        return cls(url=url, anon_key=anon)


@dataclass
class StoredSession:
    access_token: str
    refresh_token: str
    expires_at: int
    user_id: str
    email: str
    provider: str
    # GitHub provider token — populated on GitHub OAuth only; used as the
    # "easy path" Copilot auth fallback (see obscura.core.auth.AuthConfig).
    provider_token: str | None = None
    # Provider refresh token (when Supabase returns it).
    provider_refresh_token: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "user_id": self.user_id,
            "email": self.email,
            "provider": self.provider,
            "provider_token": self.provider_token,
            "provider_refresh_token": self.provider_refresh_token,
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
            provider_token=(
                str(d["provider_token"])
                if d.get("provider_token") is not None
                else None
            ),
            provider_refresh_token=(
                str(d["provider_refresh_token"])
                if d.get("provider_refresh_token") is not None
                else None
            ),
        )


# ---------------------------------------------------------------------------
# Secure session storage
# ---------------------------------------------------------------------------
# Preferred: OS keychain via the `keyring` package (Keychain on macOS,
# Secret Service on Linux, Credential Manager on Windows). Falls back to a
# 0600 plaintext file when `keyring` isn't installed or its backend can't
# start (e.g. headless Linux without a login session).


def _keyring_available() -> bool:
    try:
        import keyring  # noqa: F401

        backend = keyring.get_keyring()
        # NullKeyring / FailKeyring don't actually persist — treat as absent.
        name = type(backend).__name__
        if name in {"NullKeyring", "FailKeyring"}:
            return False
    except Exception:
        logger.debug("suppressed exception in _keyring_available", exc_info=True)
        return False
    return True


def save_session(session: StoredSession) -> None:
    payload = json.dumps(session.to_dict(), indent=2)

    if _keyring_available():
        try:
            import keyring

            keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, payload)
            # Drop any plaintext left behind from a prior run.
            if CREDENTIALS_PATH.exists():
                try:
                    CREDENTIALS_PATH.unlink()
                except OSError:
                    logger.debug("suppressed exception in save_session", exc_info=True)
            return
        except Exception as exc:
            logger.warning(
                "Keyring write failed (%s); falling back to plaintext %s",
                exc,
                CREDENTIALS_PATH,
            )

    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.write_text(payload)
    try:
        CREDENTIALS_PATH.chmod(0o600)
    except OSError:
        logger.debug("suppressed exception in save_session", exc_info=True)


def load_session() -> StoredSession | None:
    if _keyring_available():
        try:
            import keyring

            raw = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
            if raw:
                try:
                    return StoredSession.from_dict(json.loads(raw))
                except (ValueError, KeyError):
                    logger.debug("suppressed exception in load_session", exc_info=True)
                    return None
        except Exception as exc:
            logger.debug("Keyring read failed: %s", exc)

    if not CREDENTIALS_PATH.exists():
        return None
    try:
        return StoredSession.from_dict(json.loads(CREDENTIALS_PATH.read_text()))
    except (OSError, ValueError, KeyError):
        logger.debug("suppressed exception in load_session", exc_info=True)
        return None


def clear_session() -> bool:
    removed = False
    if _keyring_available():
        try:
            import keyring
            import keyring.errors

            try:
                keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
                removed = True
            except keyring.errors.PasswordDeleteError:
                logger.debug("suppressed exception in clear_session", exc_info=True)
        except Exception as exc:
            logger.debug("Keyring delete failed: %s", exc)

    if CREDENTIALS_PATH.exists():
        CREDENTIALS_PATH.unlink()
        removed = True
    return removed


# ---------------------------------------------------------------------------
# Provider-secret sync (Supabase user_metadata)
# ---------------------------------------------------------------------------


def _provider_secret_payload(session: StoredSession) -> dict[str, str]:
    payload: dict[str, str] = {}
    if session.provider_token:
        payload["provider_token"] = session.provider_token
    if session.provider_refresh_token:
        payload["provider_refresh_token"] = session.provider_refresh_token
    return payload


def _build_provider_secrets_metadata(
    *,
    existing_user_metadata: dict[str, Any] | None,
    provider: str,
    session: StoredSession,
) -> dict[str, Any]:
    metadata = dict(existing_user_metadata or {})
    existing_secrets = metadata.get(_PROVIDER_SECRET_METADATA_KEY)
    provider_secrets: dict[str, Any]
    if isinstance(existing_secrets, dict):
        provider_secrets = dict(cast(dict[str, Any], existing_secrets))
    else:
        provider_secrets = {}

    merged = _provider_secret_payload(session)
    if merged:
        provider_secrets[provider] = {**provider_secrets.get(provider, {}), **merged}
        metadata[_PROVIDER_SECRET_METADATA_KEY] = provider_secrets

    return metadata


def sync_provider_secrets_to_supabase(
    cfg: SupabaseCliConfig,
    *,
    provider: str,
    session: StoredSession,
) -> None:
    service_role_key = (_secrets.resolve("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not service_role_key:
        return
    if not session.user_id:
        return

    admin_headers = {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Content-Type": "application/json",
    }

    user_resp = httpx.get(
        f"{cfg.url}/auth/v1/admin/users/{session.user_id}",
        headers=admin_headers,
        timeout=20.0,
    )
    if user_resp.status_code != 200:
        raise RuntimeError(
            f"Failed to fetch Supabase user metadata ({user_resp.status_code}): {user_resp.text}",
        )

    user_body: dict[str, Any] = cast(dict[str, Any], user_resp.json())
    existing_user_metadata = user_body.get("user_metadata")
    metadata = _build_provider_secrets_metadata(
        existing_user_metadata=(
            cast(dict[str, Any], existing_user_metadata)
            if isinstance(existing_user_metadata, dict)
            else None
        ),
        provider=provider,
        session=session,
    )

    if metadata == existing_user_metadata:
        return

    update_resp = httpx.put(
        f"{cfg.url}/auth/v1/admin/users/{session.user_id}",
        headers=admin_headers,
        json={"user_metadata": metadata},
        timeout=20.0,
    )
    if update_resp.status_code != 200:
        raise RuntimeError(
            f"Failed to update Supabase user metadata ({update_resp.status_code}): {update_resp.text}",
        )


# ---------------------------------------------------------------------------
# Refresh + token accessor
# ---------------------------------------------------------------------------


def _refresh_session(cfg: SupabaseCliConfig, refresh_token: str) -> StoredSession:
    resp = httpx.post(
        f"{cfg.url}/auth/v1/token",
        params={"grant_type": "refresh_token"},
        headers={"apikey": cfg.anon_key, "Content-Type": "application/json"},
        json={"refresh_token": refresh_token},
        timeout=20.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"refresh failed ({resp.status_code}): {resp.text}")
    body: dict[str, Any] = cast(dict[str, Any], resp.json())
    user_raw: Any = body.get("user") or {}
    user: dict[str, Any] = (
        cast(dict[str, Any], user_raw) if isinstance(user_raw, dict) else {}
    )
    provider_token = body.get("provider_token")
    provider_refresh_token = body.get("provider_refresh_token")
    return StoredSession(
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        expires_at=int(time.time()) + int(body.get("expires_in", 3600)),
        user_id=str(user.get("id", "")),
        email=str(user.get("email", "")),
        provider="refresh",
        provider_token=str(provider_token) if provider_token else None,
        provider_refresh_token=(
            str(provider_refresh_token) if provider_refresh_token else None
        ),
    )


def get_access_token() -> str | None:
    """Return a valid access token, refreshing if needed.

    Public helper for API clients that want to authenticate as the current
    user. Returns ``None`` when no session is stored or Supabase isn't
    configured.
    """
    session = load_session()
    if session is None:
        return None

    if session.expires_at - REFRESH_LEEWAY_SECONDS > int(time.time()):
        return session.access_token

    cfg = SupabaseCliConfig.from_env()
    if cfg is None:
        return session.access_token if session.expires_at > int(time.time()) else None

    try:
        refreshed = _refresh_session(cfg, session.refresh_token)
    except Exception as exc:
        logger.debug("CLI token refresh failed: %s", exc)
        return None
    refreshed.provider = session.provider
    # Preserve previously-captured provider secrets — Supabase refresh
    # responses may omit them.
    if refreshed.provider_token is None:
        refreshed.provider_token = session.provider_token
    if refreshed.provider_refresh_token is None:
        refreshed.provider_refresh_token = session.provider_refresh_token
    save_session(refreshed)
    if refreshed.provider == "github":
        sync_provider_secrets_to_supabase(cfg, provider="github", session=refreshed)
    return refreshed.access_token


def get_github_token() -> str | None:
    """Return the stored Supabase-forwarded GitHub OAuth token, if any.

    This is the CLI-side source for the "easy path" Copilot fallback — see
    :class:`obscura.core.auth.AuthConfig.oauth_github_token`. Only populated
    after a GitHub OAuth sign-in; magic-link sessions return ``None``.
    """
    session = load_session()
    return session.provider_token if session else None


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode JWT payload without verifying — caller must trust the source."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))  # type: ignore[no-any-return]
    except Exception:
        logger.debug("suppressed exception in decode_jwt_payload", exc_info=True)
        return {}


__all__ = [
    "CREDENTIALS_PATH",
    "REFRESH_LEEWAY_SECONDS",
    "StoredSession",
    "SupabaseCliConfig",
    "clear_session",
    "get_access_token",
    "get_github_token",
    "load_session",
    "save_session",
]
