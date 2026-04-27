"""OS-keyring-backed credential storage for Obscura.

This module is the *safer default* for customers who don't want their
upstream LLM credentials living as plaintext in environment variables.
SOC2 C1 expects credentials to be protected at rest; env-var posture
leaks to anything that can read ``/proc/self/environ``. Keyring
(Keychain on macOS, Secret Service on Linux, Credential Manager on
Windows) stores them in a per-user encrypted store gated by the
platform's own auth.

This module is intentionally thin — it's a namespaced wrapper over the
optional ``keyring`` package. When the package isn't installed (or the
platform has no usable backend), every call degrades cleanly to
``None`` / no-op. Callers downstream keep working off env vars.

Secret naming convention:
    service = "obscura"
    account = "<provider>:<kind>"

Examples:
    get_secret("github:token")
    get_secret("anthropic:api_key")
    get_secret("openai:api_key")
    get_secret("moonshot:api_key")

The service/account separation matches platform conventions so these
show up sensibly in the OS keyring UI.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_SERVICE = "obscura"


# ---------------------------------------------------------------------------
# Backend probe
# ---------------------------------------------------------------------------


def _load_keyring() -> Any | None:
    """Return the keyring module, or None if it isn't importable.

    We import lazily on every call — importing keyring can cost ~50ms
    on some platforms and we don't want that on every Obscura startup
    when the feature isn't used. Callers that hit a hot path should
    cache the result themselves.
    """
    try:
        import keyring  # type: ignore[import-not-found]

        return keyring
    except ImportError:
        return None


def is_available() -> bool:
    """True iff the keyring package is installed AND a backend is usable."""
    kr = _load_keyring()
    if kr is None:
        return False
    try:
        backend = kr.get_keyring()
    except Exception:  # noqa: BLE001 — platform-specific failures
        return False
    # keyring ships a null backend on systems with no real keyring.
    # ``fail.Keyring`` and ``null.Keyring`` both refuse to store — treat
    # as unavailable so callers don't silently no-op writes.
    name = type(backend).__name__.lower()
    return "fail" not in name and "null" not in name


# ---------------------------------------------------------------------------
# Read / write / list / delete
# ---------------------------------------------------------------------------


def get_secret(name: str) -> str | None:
    """Return the stored secret for ``name`` or None if absent/unavailable."""
    kr = _load_keyring()
    if kr is None:
        return None
    try:
        value = kr.get_password(_SERVICE, name)
    except Exception:  # noqa: BLE001
        logger.debug("keyring get_password failed for %s", name, exc_info=True)
        return None
    if not value:
        return None
    return value.strip() or None


def set_secret(name: str, value: str) -> bool:
    """Store ``value`` under ``name``. Returns True on success."""
    if not value or not value.strip():
        raise ValueError("cannot store empty secret")
    kr = _load_keyring()
    if kr is None:
        return False
    try:
        kr.set_password(_SERVICE, name, value.strip())
        return True
    except Exception:  # noqa: BLE001
        logger.warning("keyring set_password failed for %s", name, exc_info=True)
        return False


def delete_secret(name: str) -> bool:
    """Remove the stored secret for ``name``. Returns True if anything was removed."""
    kr = _load_keyring()
    if kr is None:
        return False
    try:
        kr.delete_password(_SERVICE, name)
        return True
    except Exception:  # noqa: BLE001
        # keyring raises PasswordDeleteError when the account isn't present;
        # treat that as a quiet "nothing to do".
        logger.debug("keyring delete_password failed for %s", name, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Known accounts — the provider-resolver layer uses these
# ---------------------------------------------------------------------------

#: Map of canonical secret names → the human-friendly label an operator
#: sees in the CLI. Enumerated here (not computed from free strings) so
#: ``obscura admin secret list`` is scoped to secrets we actually use.
KNOWN_SECRETS: tuple[tuple[str, str], ...] = (
    ("github:token", "GitHub token (Copilot backend)"),
    ("anthropic:api_key", "Anthropic API key (Claude backend)"),
    ("openai:api_key", "OpenAI API key (OpenAI + Codex backends)"),
    ("moonshot:api_key", "Moonshot / Kimi API key"),
    ("obscura:db_key", "SQLCipher database key (if encryption extra in use)"),
)


def list_stored() -> list[str]:
    """Return names of the KNOWN secrets that currently have a value set."""
    return [name for name, _label in KNOWN_SECRETS if get_secret(name)]
