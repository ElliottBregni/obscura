"""obscura.auth.supabase_secrets -- User-scoped encrypted cloud secrets bags.

Two vaults live in ``user_metadata``, each with homogeneous encryption:

* ``obscura_vault`` — regular keys. Fernet-encrypted with a key derived
  from the user's email + a per-user salt. Zero-friction: no passphrase,
  just log in and it works. Email is public-ish so this is
  obfuscation-grade; the real protection is Supabase's auth on the row.
* ``obscura_vault_risk`` — "risky" keys the operator explicitly opted
  into passphrase protection for (via ``cloud push NAME --risk``).
  Fernet-encrypted with a key derived from a user-chosen passphrase +
  a separate salt. The passphrase is never stored -- only the derived
  key lives in the OS keyring, and the operator re-enters the
  passphrase if they clear it.

**Precedence on read.** When ``get(NAME)`` is called, we try the regular
vault first (always decryptable from the session) and fall back to the
risky vault if the name is there. Entries can only live in one vault at
a time -- ``push`` writes to the chosen vault and deletes from the
other, so there's no ambiguity.

**What gets blocked from cloud entirely.**

:data:`_NEVER_PUSH` names (Supabase bootstrap + server-only secrets)
are refused at the client level before any network call.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
from cryptography.fernet import Fernet, InvalidToken

from obscura.auth.secrets import KEYRING_SERVICE as _KEYRING_SERVICE
from obscura.auth.secrets import append_audit as _append_audit

logger = logging.getLogger(__name__)

# user_metadata keys.
_REGULAR_VAULT_KEY = "obscura_vault"
_RISKY_VAULT_KEY = "obscura_vault_risk"
_REGULAR_SALT_KEY = "obscura_vault_salt"
_RISKY_SALT_KEY = "obscura_vault_risk_salt"

# OS keyring slot for the passphrase-derived key. Separate from session
# storage so clearing the session doesn't wipe the passphrase key and
# vice versa.
_PASSPHRASE_KEY_SLOT = "cloud-passphrase-key"

# scrypt parameters. N=2^14 fits under OpenSSL's default 32 MB memory
# cap and derives in ~50ms.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32

# Names that must NEVER be pushed to either vault.
_NEVER_PUSH: frozenset[str] = frozenset(
    {
        "SUPABASE_URL",
        "SUPABASE_ANON_KEY",
        "SUPABASE_JWT_SECRET",
        "SUPABASE_SERVICE_ROLE_KEY",
    },
)

_REQUEST_TIMEOUT = 10.0


class SupabaseVaultError(RuntimeError):
    """Raised on explicit vault operation failures."""


class VaultPushBlocked(SupabaseVaultError):
    """Attempted to push a name in :data:`_NEVER_PUSH`."""


class PassphraseRequired(SupabaseVaultError):
    """The caller tried to touch the risky vault without a cached passphrase key.

    CLI commands catch this and prompt; the resolver tier catches it
    and falls through silently.
    """


# ---------------------------------------------------------------------------
# Crypto primitives
# ---------------------------------------------------------------------------


def _derive_key(key_material: str, salt: bytes) -> bytes:
    raw = hashlib.scrypt(
        key_material.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_LEN,
    )
    return base64.urlsafe_b64encode(raw)


def _generate_salt() -> bytes:
    import secrets as py_secrets

    return py_secrets.token_bytes(16)


def _encode_salt(salt: bytes) -> str:
    return base64.urlsafe_b64encode(salt).decode("ascii")


def _decode_salt(encoded: str) -> bytes:
    return base64.urlsafe_b64decode(encoded.encode("ascii"))


# ---------------------------------------------------------------------------
# Passphrase key caching (OS keyring)
# ---------------------------------------------------------------------------


def _load_passphrase_key() -> bytes | None:
    """Read the cached passphrase-derived Fernet key from the OS keyring."""
    try:
        import keyring

        raw = keyring.get_password(_KEYRING_SERVICE, _PASSPHRASE_KEY_SLOT)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Keyring read of passphrase key failed: %s", exc)
        return None
    if not raw:
        return None
    return raw.encode("ascii")


def _store_passphrase_key(key: bytes) -> bool:
    try:
        import keyring

        keyring.set_password(
            _KEYRING_SERVICE,
            _PASSPHRASE_KEY_SLOT,
            key.decode("ascii"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Keyring write of passphrase key failed: %s", exc)
        return False
    return True


def _clear_passphrase_key() -> bool:
    try:
        import keyring
        import keyring.errors

        try:
            keyring.delete_password(_KEYRING_SERVICE, _PASSPHRASE_KEY_SLOT)
        except keyring.errors.PasswordDeleteError:
            return False
    except Exception as exc:  # noqa: BLE001
        logger.debug("Keyring delete of passphrase key failed: %s", exc)
        return False
    return True


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass
class SupabaseVaultClient:
    """Two-vault client: regular (email-derived key) + risky (passphrase)."""

    api_url: str
    anon_key: str
    get_access_token: Callable[[], str | None]
    _cache: dict[str, str] | None = field(default=None, init=False, repr=False)
    _fetch_attempted: bool = field(default=False, init=False, repr=False)
    _email: str | None = field(default=None, init=False, repr=False)
    _email_key: bytes | None = field(default=None, init=False, repr=False)
    _passphrase_key: bytes | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    # -- resolver-safe methods ---------------------------------------------

    def get(self, name: str) -> str | None:
        bag = self._ensure_cache()
        if bag is None:
            return None
        return bag.get(name)

    def snapshot(self) -> dict[str, str]:
        bag = self._ensure_cache()
        return dict(bag) if bag else {}

    # -- explicit methods --------------------------------------------------

    def push(self, name: str, value: str, *, risk: bool = False) -> None:
        """Encrypt *value* and write to the chosen vault.

        ``risk=True`` routes to the passphrase-protected vault; callers
        must have either called :meth:`set_passphrase` this session or
        left a cached key in the OS keyring from a previous run.
        """
        if name in _NEVER_PUSH:
            raise VaultPushBlocked(
                f"Refusing to push {name} to the cloud vault: this name "
                "is in the hard-blocked set (public config or a "
                "server-only secret that must not leave the server).",
            )

        token = self._require_token()
        metadata = self._fetch_user_metadata(token)

        if risk:
            key = self._require_passphrase_key(token, metadata)
            target_vault_field = _RISKY_VAULT_KEY
            other_vault_field = _REGULAR_VAULT_KEY
        else:
            key = self._require_email_key(token, metadata)
            target_vault_field = _REGULAR_VAULT_KEY
            other_vault_field = _RISKY_VAULT_KEY

        target = self._extract_vault(metadata, target_vault_field)
        target[name] = Fernet(key).encrypt(value.encode("utf-8")).decode("ascii")

        # Ensure the name doesn't live in the other vault with a stale
        # (and now incorrect) value. push is "set this one here".
        other = self._extract_vault(metadata, other_vault_field)
        other.pop(name, None)

        self._write_metadata(
            token,
            metadata,
            regular_vault=(
                target if target_vault_field == _REGULAR_VAULT_KEY else other
            ),
            risky_vault=(target if target_vault_field == _RISKY_VAULT_KEY else other),
        )
        self._invalidate_cache()

        _append_audit(
            {
                "ts": _utc_now_iso(),
                "event": "cloud_push",
                "name": name,
                "risk": risk,
            },
        )

    def delete(self, name: str) -> bool:
        """Remove *name* from whichever vault it's in. True if something was removed."""
        token = self._require_token()
        metadata = self._fetch_user_metadata(token)
        regular = self._extract_vault(metadata, _REGULAR_VAULT_KEY)
        risky = self._extract_vault(metadata, _RISKY_VAULT_KEY)

        removed = False
        if name in regular:
            del regular[name]
            removed = True
        if name in risky:
            del risky[name]
            removed = True
        if not removed:
            return False

        self._write_metadata(
            token,
            metadata,
            regular_vault=regular,
            risky_vault=risky,
        )
        self._invalidate_cache()

        _append_audit(
            {
                "ts": _utc_now_iso(),
                "event": "cloud_delete",
                "name": name,
            },
        )
        return True

    def names(self) -> list[tuple[str, bool]]:
        """Return ``[(name, is_risky)]`` for everything stored.

        Does not require any key -- used by ``cloud status`` to show
        the inventory without decrypting.
        """
        token = self._require_token()
        metadata = self._fetch_user_metadata(token)
        regular = self._extract_vault(metadata, _REGULAR_VAULT_KEY)
        risky = self._extract_vault(metadata, _RISKY_VAULT_KEY)
        out = [(n, False) for n in regular] + [(n, True) for n in risky]
        return sorted(out)

    def set_passphrase(self, passphrase: str) -> None:
        """Derive and cache the passphrase key.

        Fetches (or creates) the risky-vault salt from Supabase, runs
        KDF, caches the result in both memory and the OS keyring.
        Prompt UX is handled by the caller.
        """
        token = self._require_token()
        metadata = self._fetch_user_metadata(token)
        salt = self._ensure_salt(
            token,
            metadata,
            _RISKY_SALT_KEY,
            create_if_missing=True,
        )
        key = _derive_key(passphrase, cast("bytes", salt))
        with self._lock:
            self._passphrase_key = key
        _store_passphrase_key(key)

    def clear_passphrase(self) -> None:
        """Forget the cached passphrase key on this machine."""
        with self._lock:
            self._passphrase_key = None
        _clear_passphrase_key()

    def has_passphrase_key(self) -> bool:
        """True when we could decrypt risky entries without re-prompting."""
        if self._passphrase_key is not None:
            return True
        return _load_passphrase_key() is not None

    def has_risky_entries(self) -> bool:
        """True when the risky vault has at least one entry.

        Useful for CLI flows that want to skip prompting for the
        passphrase when there's nothing there to decrypt.
        """
        token = self._require_token()
        metadata = self._fetch_user_metadata(token)
        return bool(self._extract_vault(metadata, _RISKY_VAULT_KEY))

    # ----------------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------------

    def _require_token(self) -> str:
        token = self.get_access_token()
        if not token:
            raise SupabaseVaultError(
                "No valid Supabase session. Run `obscura-auth login` first.",
            )
        return token

    def _invalidate_cache(self) -> None:
        with self._lock:
            self._cache = None
            self._fetch_attempted = False

    def _ensure_cache(self) -> dict[str, str] | None:
        with self._lock:
            if self._cache is not None:
                return self._cache
            if self._fetch_attempted:
                return None
            self._fetch_attempted = True
            try:
                token = self.get_access_token()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Supabase token fetch failed: %s", exc)
                return None
            if not token:
                return None
            try:
                metadata = self._fetch_user_metadata(token)
                out: dict[str, str] = {}

                # Regular vault -- always decryptable if salt exists.
                email_key = self._resolve_email_key(
                    token,
                    metadata,
                    create_salt_if_missing=False,
                )
                if email_key is not None:
                    out.update(
                        self._decrypt_vault(email_key, metadata, _REGULAR_VAULT_KEY),
                    )

                # Risky vault -- only if passphrase key is cached.
                passphrase_key = self._load_passphrase_key_cached()
                if passphrase_key is not None:
                    out.update(
                        self._decrypt_vault(
                            passphrase_key,
                            metadata,
                            _RISKY_VAULT_KEY,
                        ),
                    )

                self._cache = out
            except Exception as exc:  # noqa: BLE001
                logger.debug("Supabase vault fetch failed: %s", exc)
                self._cache = None
            return self._cache

    def _resolve_email_key(
        self,
        token: str,
        metadata: dict[str, Any],
        *,
        create_salt_if_missing: bool,
    ) -> bytes | None:
        if self._email_key is not None:
            return self._email_key

        email = self._email or self._extract_email_from_user(token)
        if not email:
            raise SupabaseVaultError(
                "Supabase session has no email; cannot derive vault key.",
            )
        self._email = email

        salt = self._ensure_salt(
            token,
            metadata,
            _REGULAR_SALT_KEY,
            create_if_missing=create_salt_if_missing,
        )
        if salt is None:
            return None
        self._email_key = _derive_key(email, salt)
        return self._email_key

    def _require_email_key(
        self,
        token: str,
        metadata: dict[str, Any],
    ) -> bytes:
        key = self._resolve_email_key(
            token,
            metadata,
            create_salt_if_missing=True,
        )
        assert key is not None  # create_salt_if_missing=True → never None
        return key

    def _require_passphrase_key(
        self,
        token: str,
        metadata: dict[str, Any],
    ) -> bytes:
        key = self._load_passphrase_key_cached()
        if key is None:
            raise PassphraseRequired(
                "The --risk flag requires a passphrase. Call "
                "set_passphrase() first or use the CLI's interactive "
                "prompt.",
            )
        # Validate the cached key matches the current risky-vault salt:
        # if the user has rotated or cleared on another machine, the
        # key might be stale. Re-derive and compare is too expensive
        # without the passphrase itself, so we trust the cache and let
        # decrypt failures surface naturally.
        _ = metadata  # salt check deferred to decrypt time
        _ = token
        return key

    def _load_passphrase_key_cached(self) -> bytes | None:
        if self._passphrase_key is not None:
            return self._passphrase_key
        cached = _load_passphrase_key()
        if cached is not None:
            self._passphrase_key = cached
        return cached

    def _ensure_salt(
        self,
        token: str,
        metadata: dict[str, Any],
        field_key: str,
        *,
        create_if_missing: bool,
    ) -> bytes | None:
        encoded = metadata.get(field_key)
        if isinstance(encoded, str) and encoded.strip():
            return _decode_salt(encoded.strip())
        if not create_if_missing:
            return None
        salt = _generate_salt()
        metadata[field_key] = _encode_salt(salt)
        self._write_metadata(token, metadata)
        return salt

    def _extract_email_from_user(self, token: str) -> str | None:
        resp = httpx.get(
            f"{self.api_url}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": self.anon_key,
            },
            timeout=_REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            raise SupabaseVaultError(
                f"Supabase GET /auth/v1/user failed ({resp.status_code}): {resp.text}",
            )
        email = resp.json().get("email")
        return email if isinstance(email, str) and email.strip() else None

    def _fetch_user_metadata(self, token: str) -> dict[str, Any]:
        resp = httpx.get(
            f"{self.api_url}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": self.anon_key,
            },
            timeout=_REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            raise SupabaseVaultError(
                f"Supabase GET /auth/v1/user failed ({resp.status_code}): {resp.text}",
            )
        body = resp.json()
        email = body.get("email")
        if isinstance(email, str) and email.strip():
            self._email = email
        user_metadata = body.get("user_metadata")
        if not isinstance(user_metadata, dict):
            return {}
        return cast("dict[str, Any]", user_metadata)

    def _extract_vault(
        self,
        metadata: Mapping[str, Any],
        field_key: str,
    ) -> dict[str, str]:
        vault_any = metadata.get(field_key)
        if not isinstance(vault_any, dict):
            return {}
        vault = cast("dict[Any, Any]", vault_any)
        out: dict[str, str] = {}
        for k_any, v_any in vault.items():
            if isinstance(k_any, str) and isinstance(v_any, str):
                out[k_any] = v_any
        return out

    def _decrypt_vault(
        self,
        key: bytes,
        metadata: Mapping[str, Any],
        field_key: str,
    ) -> dict[str, str]:
        vault = self._extract_vault(metadata, field_key)
        fernet = Fernet(key)
        out: dict[str, str] = {}
        for name, token in vault.items():
            try:
                plaintext = fernet.decrypt(token.encode("ascii")).decode("utf-8")
            except (InvalidToken, UnicodeDecodeError) as exc:
                logger.debug("Vault decrypt for %s failed: %s", name, exc)
                continue
            out[name] = plaintext
        return out

    def _write_metadata(
        self,
        token: str,
        current: Mapping[str, Any],
        *,
        regular_vault: Mapping[str, str] | None = None,
        risky_vault: Mapping[str, str] | None = None,
    ) -> None:
        merged: dict[str, Any] = dict(current)
        if regular_vault is not None:
            merged[_REGULAR_VAULT_KEY] = dict(regular_vault)
        if risky_vault is not None:
            merged[_RISKY_VAULT_KEY] = dict(risky_vault)
        resp = httpx.put(
            f"{self.api_url}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": self.anon_key,
                "Content-Type": "application/json",
            },
            json={"data": merged},
            timeout=_REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            raise SupabaseVaultError(
                f"Supabase PUT /auth/v1/user failed ({resp.status_code}): {resp.text}",
            )


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_singleton_lock = threading.Lock()
_singleton: SupabaseVaultClient | None = None
_singleton_config: tuple[str, str] | None = None


def get_client() -> SupabaseVaultClient | None:
    global _singleton, _singleton_config

    from obscura.cli.auth_commands import SupabaseCliConfig, get_access_token

    cfg = SupabaseCliConfig.from_env()
    if cfg is None:
        with _singleton_lock:
            _singleton = None
            _singleton_config = None
        return None

    config_tuple = (cfg.url, cfg.anon_key)
    with _singleton_lock:
        if _singleton is not None and _singleton_config == config_tuple:
            return _singleton
        _singleton = SupabaseVaultClient(
            api_url=cfg.url,
            anon_key=cfg.anon_key,
            get_access_token=get_access_token,
        )
        _singleton_config = config_tuple
        return _singleton


def reset() -> None:
    global _singleton, _singleton_config

    with _singleton_lock:
        _singleton = None
        _singleton_config = None


# Public aliases for tests and future passphrase UX layers.
derive_key = _derive_key
generate_salt = _generate_salt
encode_salt = _encode_salt
decode_salt = _decode_salt
load_passphrase_key = _load_passphrase_key
store_passphrase_key = _store_passphrase_key
clear_passphrase_key = _clear_passphrase_key


__all__ = [
    "PassphraseRequired",
    "SupabaseVaultClient",
    "SupabaseVaultError",
    "VaultPushBlocked",
    "clear_passphrase_key",
    "decode_salt",
    "derive_key",
    "encode_salt",
    "generate_salt",
    "get_client",
    "load_passphrase_key",
    "reset",
    "store_passphrase_key",
]
