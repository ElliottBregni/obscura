"""obscura.auth.cli_user — Synthetic AuthenticatedUser for CLI/daemon contexts.

Used by tools, daemons, and background workers that run outside of a FastAPI
request and therefore have no real authenticated user. Wraps the local OS
user in an :class:`AuthenticatedUser` with the ``operator`` role so that
profile, goal, and vector-memory code paths can scope state per-machine.
"""

from __future__ import annotations

import os

from obscura.auth.models import AuthenticatedUser


def local_cli_user() -> AuthenticatedUser:
    """Return an AuthenticatedUser representing the local CLI/daemon caller."""
    return AuthenticatedUser(
        user_id=os.environ.get("USER", "local"),
        email="cli@obscura.local",
        roles=("operator",),
        org_id="local",
        token_type="user",
        raw_token="",
    )
