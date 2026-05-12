"""obscura.integrations.messaging.cred_cipher — at-rest encryption for channel credentials.

Uses Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256).  The key is
derived from a 32-byte random secret stored at ``~/.obscura/channel-creds.key``
(created on first use, mode 0o600).  Encryption is transparent: callers pass
a plain ``dict`` and get back a ``dict``; the SQLite column stores a JSON
envelope ``{"v":1,"ct":"<base64-fernet-token>"}``.

If the key file is missing on read (e.g., database was copied without the key),
``decrypt_credentials`` returns an empty dict and logs a warning rather than
crashing.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_KEY_PATH = Path.home() / ".obscura" / "channel-creds.key"
_ENVELOPE_VERSION = 1


def _get_or_create_key() -> bytes:
    """Load or generate the Fernet key."""
    if _KEY_PATH.exists():
        raw = _KEY_PATH.read_bytes().strip()
        if len(raw) == 44:  # base64url-encoded 32 bytes
            return raw
    # Generate new key
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _KEY_PATH.write_bytes(key)
    os.chmod(_KEY_PATH, 0o600)
    logger.info("cred_cipher: generated new channel-creds key at %s", _KEY_PATH)
    return key


def encrypt_credentials(creds: dict) -> str:
    """Encrypt *creds* dict and return a JSON envelope string for SQLite storage."""
    try:
        from cryptography.fernet import Fernet

        key = _get_or_create_key()
        f = Fernet(key)
        plaintext = json.dumps(creds, ensure_ascii=True).encode()
        ct = f.encrypt(plaintext).decode()
        return json.dumps({"v": _ENVELOPE_VERSION, "ct": ct})
    except Exception:
        logger.exception("cred_cipher: encryption failed — storing plaintext fallback")
        return json.dumps(creds, ensure_ascii=True)


def decrypt_credentials(stored: str) -> dict:
    """Decrypt a stored credentials string back to a dict.

    Handles both encrypted envelopes (``{"v":1,"ct":"..."}`` ) and legacy
    plaintext JSON so the migration is transparent.
    """
    if not stored:
        return {}
    try:
        obj = json.loads(stored)
    except Exception:
        return {}

    if not isinstance(obj, dict):
        return {}

    # Encrypted envelope
    if obj.get("v") == _ENVELOPE_VERSION and "ct" in obj:
        try:
            from cryptography.fernet import Fernet, InvalidToken  # noqa: F401

            key = _get_or_create_key()
            f = Fernet(key)
            plaintext = f.decrypt(obj["ct"].encode())
            return json.loads(plaintext)
        except Exception:
            logger.warning(
                "cred_cipher: decryption failed — credentials unavailable; "
                "check that %s is present and correct",
                _KEY_PATH,
            )
            return {}

    # Legacy plaintext — return as-is (will be re-encrypted on next write)
    return obj
