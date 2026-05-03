"""obscura.auth.cli_user — AuthenticatedUser for the CLI/daemon, sourced
from the Supabase session stored by ``obscura-auth login``.

The session is persisted by :mod:`obscura.auth.cli_session` after a
successful SSO login against ``auth.modernized-ai.com``. This helper loads
it and projects the JWT claims into the :class:`AuthenticatedUser` shape
used by tools, daemons, and background workers that run outside a FastAPI
request — so CLI and HTTP code paths share one notion of "the user".

Run ``obscura-auth login`` first if no session is stored.
"""

from __future__ import annotations

import base64
import json
from typing import Any, cast

from obscura.auth.cli_session import get_access_token, load_session
from obscura.auth.models import AuthenticatedUser
from obscura.auth.supabase import extract_roles


class CliAuthError(RuntimeError):
    """Raised when no authenticated CLI session is available."""


def current_cli_user() -> AuthenticatedUser:
    """Return the AuthenticatedUser from the stored auth.modernized-ai.com session.

    Raises :class:`CliAuthError` if no session is stored — run
    ``obscura-auth login`` first.
    """
    session = load_session()
    if session is None:
        raise CliAuthError(
            "No Supabase session found. Run `obscura-auth login` first to "
            "authenticate against auth.modernized-ai.com.",
        )

    # get_access_token() refreshes when expired and falls back to the cached
    # token when Supabase isn't configured for refresh.
    token = get_access_token() or session.access_token

    # Decode the JWT body unverified to harvest role / org / session claims.
    # We trust it locally because we wrote it after a verified OAuth round-trip;
    # over-the-wire calls still go through the server-side verifier.
    claims = _decode_jwt_unverified(token) or {}

    app_metadata_any: Any = claims.get("app_metadata")
    app_metadata: dict[str, Any] = (
        cast(dict[str, Any], app_metadata_any)
        if isinstance(app_metadata_any, dict)
        else {}
    )

    # Reuse the same role-mapping the FastAPI middleware applies, so CLI and
    # HTTP produce identical role tuples for the same Supabase user.
    roles = extract_roles(app_metadata, claims)
    org_id_raw = app_metadata.get("org_id")
    org_id = str(org_id_raw) if org_id_raw is not None else None
    aal = claims.get("aal")
    session_id = claims.get("session_id")

    return AuthenticatedUser(
        user_id=session.user_id,
        email=session.email,
        roles=roles,
        org_id=org_id,
        token_type="user",
        raw_token=token,
        aal=str(aal) if aal else None,
        session_id=str(session_id) if session_id else None,
    )


def _decode_jwt_unverified(token: str) -> dict[str, Any] | None:
    """Decode a JWT body without verifying the signature."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        body = base64.urlsafe_b64decode(payload + padding)
        decoded = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return None
    if isinstance(decoded, dict):
        return cast(dict[str, Any], decoded)
    return None
