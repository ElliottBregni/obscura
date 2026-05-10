"""obscura.integrations.a2a.token_manager — Centralised A2A token management.

Provides :class:`A2ATokenManager` for loading and rotating the two secrets
used by the A2A bridge:

* **OpenClaw token** — bearer token accepted by the OpenClaw gateway.
* **Obscura A2A token** — bearer token required by the Obscura A2A server for
  inbound calls from peers (e.g. OpenClaw → Obscura).

Resolution order for each token follows the principle of *environment variable
first, file fallback*: secrets committed to config files are only read when no
environment override is present.

Usage::

    from obscura.integrations.a2a.token_manager import A2ATokenManager

    mgr = A2ATokenManager()
    openclaw_token = mgr.load_openclaw_token()   # None if not configured
    a2a_token      = mgr.load_a2a_token()        # None if not configured
    new_token      = mgr.rotate_a2a_token()      # generates + persists new token
    mgr.write_env_template(Path("~/.obscura/.env.a2a.example"))
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OPENCLAW_TOKEN_ENV = "OPENCLAW_TOKEN"
_A2A_TOKEN_ENV = "OBSCURA_A2A_TOKEN"

_OPENCLAW_CONFIG_PATH = Path.home() / ".openclaw" / "openclaw.json"
_A2A_TOKEN_PATH = Path.home() / ".obscura" / "a2a-gateway.token"

_ENV_TEMPLATE = """\
# A2A bridge tokens — copy to ~/.obscura/.env and fill in values
OPENCLAW_TOKEN=<token from ~/.openclaw/openclaw.json gateway.auth.token>
OBSCURA_A2A_TOKEN=<token from ~/.obscura/a2a-gateway.token>
"""


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class A2ATokenManager:
    """Load and rotate A2A bridge tokens.

    All methods are synchronous and side-effect-free except
    :meth:`rotate_a2a_token` (writes to disk) and
    :meth:`write_env_template` (writes to the given path).
    """

    # -- OpenClaw token -------------------------------------------------------

    def load_openclaw_token(self) -> str | None:
        """Return the OpenClaw bearer token, or ``None`` if not configured.

        Resolution order:

        1. ``OPENCLAW_TOKEN`` environment variable.
        2. ``~/.openclaw/openclaw.json`` → ``gateway.auth.token``.
        """
        env_token = os.environ.get(_OPENCLAW_TOKEN_ENV, "").strip()
        if env_token:
            return env_token

        if _OPENCLAW_CONFIG_PATH.exists():
            try:
                raw_data: object = json.loads(_OPENCLAW_CONFIG_PATH.read_text())
                data: dict[str, Any] = (
                    cast(dict[str, Any], raw_data) if isinstance(raw_data, dict) else {}
                )
                raw_gateway = data.get("gateway")
                gateway: dict[str, Any] = (
                    cast(dict[str, Any], raw_gateway) if isinstance(raw_gateway, dict) else {}
                )
                raw_auth = gateway.get("auth")
                auth: dict[str, Any] = (
                    cast(dict[str, Any], raw_auth) if isinstance(raw_auth, dict) else {}
                )
                raw_token = auth.get("token")
                token: str | None = raw_token if isinstance(raw_token, str) else None
                if token:
                    return token.strip() or None
            except Exception:
                logger.debug(
                    "Failed to parse %s",
                    _OPENCLAW_CONFIG_PATH,
                    exc_info=True,
                )

        return None

    # -- Obscura A2A token ----------------------------------------------------

    def load_a2a_token(self) -> str | None:
        """Return the Obscura A2A server bearer token, or ``None`` if not configured.

        Resolution order:

        1. ``OBSCURA_A2A_TOKEN`` environment variable.
        2. ``~/.obscura/a2a-gateway.token`` file (first non-empty line).
        """
        env_token = os.environ.get(_A2A_TOKEN_ENV, "").strip()
        if env_token:
            return env_token

        if _A2A_TOKEN_PATH.exists():
            try:
                token = _A2A_TOKEN_PATH.read_text().strip()
                if token:
                    return token
            except Exception:
                logger.debug(
                    "Failed to read %s",
                    _A2A_TOKEN_PATH,
                    exc_info=True,
                )

        return None

    # -- Token rotation -------------------------------------------------------

    def rotate_a2a_token(self) -> str:
        """Generate a new Obscura A2A token, persist it, and return it.

        Writes the new token to ``~/.obscura/a2a-gateway.token``,
        creating parent directories as needed.  The previous token is
        replaced immediately.

        Returns
        -------
        str
            The newly generated 64-hex-character token.
        """
        new_token = secrets.token_hex(32)
        _A2A_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _A2A_TOKEN_PATH.write_text(new_token + "\n")
        logger.info("Rotated A2A token → %s", _A2A_TOKEN_PATH)
        return new_token

    # -- Template generation --------------------------------------------------

    def write_env_template(self, path: Path) -> None:
        """Write a ``.env.a2a.example`` template to *path*.

        Parameters
        ----------
        path:
            Destination path.  Parent directories are created if absent.
        """
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_ENV_TEMPLATE)
        logger.debug("Wrote A2A env template → %s", path)


__all__ = ["A2ATokenManager"]
