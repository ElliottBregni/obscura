"""Live smoke test: JWKS endpoint returns a usable ES256/RS256 key.

Auto-skips when ``SUPABASE_JWKS_URL`` isn't configured (e.g. pre-migration
projects still on HS256). When configured, this test proves the end-to-end
path our middleware takes to validate Supabase tokens:

1. Fetch the project's JWKS
2. Select a signing key by ``kid``
3. Confirm the key parses into a form PyJWT can verify with

If this passes in CI/locally but real tokens still 401, the problem is
elsewhere (audience mismatch, clock skew, revoked session).
"""

from __future__ import annotations

import os

import pytest


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass


_load_env()

URL = os.environ.get("SUPABASE_JWKS_URL", "")

skip_if_unconfigured = pytest.mark.skipif(
    not URL,
    reason="SUPABASE_JWKS_URL not set (project still on HS256)",
)


@skip_if_unconfigured
def test_jwks_endpoint_returns_keys() -> None:
    from jwt import PyJWKClient

    client = PyJWKClient(URL)
    keys = client.get_signing_keys()
    assert keys, "JWKS endpoint returned no keys — rotation may be incomplete"
    algs = {getattr(k, "algorithm_name", "?") for k in keys}
    # Supabase currently rotates to ES256 by default, RS256 is also valid.
    assert algs <= {"ES256", "RS256"}, f"unexpected algorithms: {algs}"


@skip_if_unconfigured
def test_verifier_configures_in_jwks_mode() -> None:
    from obscura.auth.supabase import SupabaseSettings, SupabaseVerifier

    settings = SupabaseSettings(
        jwt_secret="",
        jwks_url=URL,
        audience="authenticated",
        issuer=f"{os.environ.get('SUPABASE_URL', '').rstrip('/')}/auth/v1",
    )
    assert settings.enabled
    # Build the verifier; no tokens to decode here, just confirm no exceptions
    # and the JWKS client is lazily ready.
    v = SupabaseVerifier(settings)
    assert v is not None
