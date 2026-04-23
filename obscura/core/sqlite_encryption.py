"""SQLite encryption wrapper for Obscura's persistent stores.

This is the single entry point for opening any SQLite database Obscura
persists confidential data into (supervisor event log, memory KV,
vector memory). The wrapper is deliberately small: it decides whether
encryption is available + configured and opens the connection with or
without ``PRAGMA key`` accordingly.

SOC2 C1 requires encryption at rest for data classified CONFIDENTIAL or
higher. Calling ``open_connection`` ensures every store that adopts
this module is encrypted when the `encrypted` extra is installed and a
key can be resolved; otherwise the module logs a loud warning and falls
back to stdlib sqlite3 so the product keeps working. Customers who
require encryption-by-default install the extra and set (or let the
module auto-generate) a key; customers who consciously run unencrypted
see the warning at startup.

Key resolution order:

1. ``OBSCURA_DB_KEY`` environment variable (explicit operator-supplied).
2. OS keyring under service ``obscura`` / account ``db-key`` (if the
   ``keyring`` package is installed and the platform supports it).
3. A file at ``~/.obscura/db.key`` written with mode 0o600 on first run
   (the user-writable fallback — never committed, never logged).

Rotating keys is out of scope for this module; that's a Phase 5 or
later exercise and requires migration tooling.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlite3 import Connection

logger = logging.getLogger(__name__)


_KEYRING_SERVICE = "obscura"
_KEYRING_ACCOUNT = "db-key"
_DEFAULT_KEY_FILE = Path.home() / ".obscura" / "db.key"
_KEY_BYTES = 32  # 256-bit key material before hex-encoding


# ---------------------------------------------------------------------------
# Backend probing
# ---------------------------------------------------------------------------


def _probe_sqlcipher() -> Any | None:
    """Return the sqlcipher3 module if available, else None.

    Callers must not cache the result — this is cheap and we want the
    availability check to reflect the current interpreter at call time,
    which matters in tests that poke sys.modules.
    """
    try:
        import sqlcipher3  # type: ignore[import-not-found]

        return sqlcipher3
    except ImportError:
        return None


def is_encryption_available() -> bool:
    """True iff a SQLCipher-compatible backend is importable."""
    return _probe_sqlcipher() is not None


# ---------------------------------------------------------------------------
# Key resolution
# ---------------------------------------------------------------------------


class KeyResolutionError(RuntimeError):
    """Raised when we cannot produce a usable database key."""


def resolve_db_key(*, create_if_missing: bool = True) -> str | None:
    """Return the hex-encoded database key, or None if encryption is off.

    Looks at the env var, then the keyring, then the fallback file.
    When nothing is found and ``create_if_missing`` is True, generates a
    new random key and persists it (keyring first, file fallback) so the
    same key is used across runs.
    """
    key = os.environ.get("OBSCURA_DB_KEY", "").strip()
    if key:
        return key

    # Keyring lookup — optional dep, treat any failure as absent.
    key = _read_keyring()
    if key:
        return key

    # File fallback
    try:
        if _DEFAULT_KEY_FILE.exists():
            return _DEFAULT_KEY_FILE.read_text().strip()
    except OSError:
        pass

    if not create_if_missing:
        return None

    # Generate, persist, return.
    new_key = secrets.token_hex(_KEY_BYTES)
    if _write_keyring(new_key):
        logger.info("Generated new database key and stored in OS keyring.")
        return new_key
    if _write_key_file(new_key):
        logger.warning(
            "OS keyring unavailable; stored database key at %s (mode 0600). "
            "Consider installing the `keyring` extra for stronger isolation.",
            _DEFAULT_KEY_FILE,
        )
        return new_key
    raise KeyResolutionError(
        "Could not persist a newly generated database key. "
        "Set OBSCURA_DB_KEY explicitly or check permissions on ~/.obscura/."
    )


def _read_keyring() -> str | None:
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        value = keyring.get_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT)
    except Exception:  # pragma: no cover — platform-specific failures
        return None
    return value or None


def _write_keyring(key: str) -> bool:
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_ACCOUNT, key)
    except Exception:
        return False
    return True


def _write_key_file(key: str) -> bool:
    """Write the key atomically at 0o600. Returns True on success."""
    try:
        _DEFAULT_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            _DEFAULT_KEY_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.write(fd, key.encode("ascii"))
        finally:
            os.close(fd)
        # Enforce mode in case an earlier file existed with different perms.
        os.chmod(_DEFAULT_KEY_FILE, 0o600)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class SqlCipherUnavailable(RuntimeError):
    """Raised when a caller asked for encryption but no backend is installed."""


def open_connection(
    path: Path | str,
    *,
    require_encryption: bool = False,
) -> Connection:
    """Open a SQLite connection, encrypted when possible.

    - If the sqlcipher3 backend is available **and** a key can be
      resolved, opens through it and issues ``PRAGMA key``.
    - If the backend isn't available and ``require_encryption`` is True,
      raises ``SqlCipherUnavailable`` so the caller can decide how to
      respond (e.g., refuse to start a confidential store).
    - Otherwise falls back to stdlib sqlite3 and logs a warning exactly
      once per path so the operator sees the posture.
    """
    path = Path(path)
    sqlcipher = _probe_sqlcipher()

    if sqlcipher is not None:
        key = resolve_db_key()
        if key:
            conn = sqlcipher.connect(str(path))  # type: ignore[reportUnknownMemberType]
            # PRAGMA key must be the very first statement. Use a
            # parameterised statement by quoting the hex literal ourselves
            # — PRAGMA doesn't take bind parameters.
            conn.execute(f"PRAGMA key = \"x'{key}'\"")
            conn.execute("PRAGMA cipher_page_size = 4096")
            conn.execute("PRAGMA kdf_iter = 64000")
            # Verify the key by forcing a read from sqlite_master; an
            # incorrect key surfaces here rather than later with a
            # mysterious "file is not a database" error.
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return conn

    if require_encryption:
        raise SqlCipherUnavailable(
            f"Encryption was required for {path} but the sqlcipher3 backend "
            "is not installed. Install the `encrypted` extra: "
            "`uv pip install 'obscura[encrypted]'`."
        )

    _warn_unencrypted_once(path)
    return sqlite3.connect(str(path))


_warned_paths: set[str] = set()


def _warn_unencrypted_once(path: Path) -> None:
    key = str(path.resolve())
    if key in _warned_paths:
        return
    _warned_paths.add(key)
    logger.warning(
        "Opening %s UNENCRYPTED. Install the `encrypted` extra to enable "
        "SQLCipher-backed at-rest encryption for SOC2 C1 compliance.",
        path,
    )


def _reset_warned_paths() -> None:
    """Testing hook."""
    _warned_paths.clear()
