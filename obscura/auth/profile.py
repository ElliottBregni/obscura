"""obscura.auth.profile -- User profile + device context stored in Supabase user_metadata.

A small, plaintext companion to the encrypted secret vaults: defaults,
preferences, and a list of machines the user has registered. Everything
here is **non-secret** -- display names, backend defaults, feature
flags, timezone, UUIDs identifying machines. Anything that would qualify
as a credential belongs in the vault, not here.

**Where it lives.** ``user_metadata.obscura_profile`` -- sibling to
``obscura_vault`` / ``obscura_vault_risk``. Reading and writing uses the
user's own Supabase session JWT via ``PATCH /auth/v1/user``.

**Machine ID.** Each machine that runs Obscura has a UUID stored at
``~/.obscura/machine.id`` (or ``$OBSCURA_HOME/machine.id``). The file is
created on first read. Registering a device copies the UUID plus some
harmless context (platform, hostname, timestamps) into
``profile.devices``; the file stays local. Wiping a laptop is a
``profile device remove <ID>`` away.

**Scope discipline.** Resist the urge to stuff anything here. Every
field must have a clear caller -- an Obscura feature that reads the
value and does something different because of it. Unused fields rot
and surprise the next reader.
"""

from __future__ import annotations

import logging
import os
import platform
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# user_metadata key. Sibling of obscura_vault / obscura_vault_risk.
_METADATA_PROFILE_KEY = "obscura_profile"

# Location of the per-machine UUID. Kept outside the profile because
# it's local-only state -- regenerating user_metadata from scratch
# shouldn't wipe the device ID, and a user removing a device from the
# cloud shouldn't invalidate the local file either.
_MACHINE_ID_FILENAME = "machine.id"

_REQUEST_TIMEOUT = 10.0


class ProfileError(RuntimeError):
    """Raised on explicit profile operation failures.

    CLI commands surface these; callers reading defaults via
    :func:`ProfileClient.get` receive ``None`` on any failure so startup
    code can't be broken by a flaky network.
    """


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class DeviceInfo(BaseModel):
    """One machine the user has registered.

    ``id`` is a UUID generated locally; the same machine keeps the same
    id across sign-ins and reinstalls as long as ``~/.obscura/machine.id``
    survives. ``name`` is a human-readable label that defaults to the
    hostname and can be renamed freely.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    platform: str
    hostname: str
    first_seen: str  # ISO 8601 UTC
    last_seen: str


class ObscuraProfile(BaseModel):
    """Per-user context bag.

    All fields are optional; missing ones fall back to env / built-in
    defaults. ``extra="forbid"`` catches typos on read back so a stale
    field name or a schema drift doesn't silently ignore user intent.
    """

    model_config = ConfigDict(extra="forbid")

    # Identity
    display_name: str | None = None
    timezone: str | None = None

    # Backend defaults -- consulted when the user runs ``obscura`` with
    # no ``-b`` / ``-m`` flags.
    default_backend: str | None = None
    default_model: str | None = None

    # Behaviour toggles
    undercover: bool | None = None
    feature_flags: list[str] = Field(default_factory=list)

    # Continuity across sessions / machines
    last_workspace: str | None = None
    last_session_id: str | None = None
    last_cwd: str | None = None

    # Machines currently signed in
    devices: list[DeviceInfo] = Field(default_factory=list)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Machine ID (~/.obscura/machine.id)
# ---------------------------------------------------------------------------


def _machine_id_path() -> Path:
    home = Path(os.environ.get("OBSCURA_HOME", str(Path.home() / ".obscura")))
    return home / _MACHINE_ID_FILENAME


def get_or_create_machine_id() -> str:
    """Return this machine's UUID, creating + persisting one on first call.

    File is written with 0600 perms to match the rest of the local
    credential surface -- machine IDs aren't secret, but no reason for
    other OS users to read them.
    """
    path = _machine_id_path()
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.debug("Machine-id read failed: %s", exc)
        else:
            if existing:
                return existing
    # Either missing or empty/corrupt -- write a fresh one.
    new_id = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_id, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return new_id


def current_device_info(name: str | None = None) -> DeviceInfo:
    """Build a :class:`DeviceInfo` for the current machine.

    Used by ``profile device register`` and ``profile device touch``.
    ``name`` defaults to the hostname; callers can pass an explicit
    label via the CLI.
    """
    host = platform.node() or "unknown-host"
    now = _utc_now_iso()
    return DeviceInfo(
        id=get_or_create_machine_id(),
        name=name or host,
        platform=platform.system().lower(),
        hostname=host,
        first_seen=now,
        last_seen=now,
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass
class ProfileClient:
    """Read/write the user's profile bag in Supabase ``user_metadata``.

    One per process (see :func:`get_client`). Caches the fetched profile
    for the life of the process so non-mutating reads are free after
    the first call. Writes invalidate the cache.
    """

    api_url: str
    anon_key: str
    get_access_token: Callable[[], str | None]
    _cache: ObscuraProfile | None = field(default=None, init=False, repr=False)
    _fetch_attempted: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    # -- resolver-safe methods ---------------------------------------------

    def get(self) -> ObscuraProfile | None:
        """Return the cached profile, fetching once if needed.

        Returns ``None`` on any failure -- matches the vault client's
        contract so this tier is equally total when plugged into
        startup-critical paths.
        """
        with self._lock:
            if self._cache is not None:
                return self._cache
            if self._fetch_attempted:
                return None
            self._fetch_attempted = True
            try:
                token = self.get_access_token()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Profile token fetch failed: %s", exc)
                return None
            if not token:
                return None
            try:
                self._cache = self._fetch(token)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Profile fetch failed: %s", exc)
                self._cache = None
            return self._cache

    # -- explicit methods --------------------------------------------------

    def load(self) -> ObscuraProfile:
        """Fetch the profile, raising on failure.

        Used by CLI commands that need to surface errors. Resolver
        paths use :meth:`get` which swallows errors.
        """
        token = self._require_token()
        self._cache = self._fetch(token)
        return self._cache

    def update(self, **fields: Any) -> ObscuraProfile:
        """Merge *fields* into the existing profile and PATCH.

        Unknown fields raise on Pydantic validation. Pass ``None`` to
        clear a scalar field; pass ``[]`` to clear a list field.
        """
        token = self._require_token()
        metadata = self._fetch_metadata(token)
        current = self._extract_profile(metadata)
        merged = current.model_copy(update=fields)
        # Round-trip through model_validate so invalid field names
        # surface as a clean Pydantic error rather than a silent merge.
        updated = ObscuraProfile.model_validate(merged.model_dump())
        self._write_profile(token, metadata, updated)
        self._cache = updated
        return updated

    def register_device(self, name: str | None = None) -> DeviceInfo:
        """Add (or refresh) the current machine's entry in ``profile.devices``.

        Idempotent -- calling twice with the same machine ID updates
        ``last_seen`` instead of duplicating. Preserves ``first_seen``
        from the existing entry when present.
        """
        token = self._require_token()
        metadata = self._fetch_metadata(token)
        current = self._extract_profile(metadata)
        fresh = current_device_info(name)

        existing_index = next(
            (i for i, d in enumerate(current.devices) if d.id == fresh.id),
            None,
        )
        if existing_index is not None:
            existing = current.devices[existing_index]
            merged = existing.model_copy(
                update={
                    "name": fresh.name if name else existing.name,
                    "platform": fresh.platform,
                    "hostname": fresh.hostname,
                    "last_seen": fresh.last_seen,
                },
            )
            devices = [*current.devices]
            devices[existing_index] = merged
            out = merged
        else:
            devices = [*current.devices, fresh]
            out = fresh

        updated = current.model_copy(update={"devices": devices})
        self._write_profile(token, metadata, updated)
        self._cache = updated
        return out

    def rename_device(self, new_name: str) -> DeviceInfo:
        """Rename the *current* machine's entry (not arbitrary ones)."""
        if not new_name.strip():
            raise ProfileError("Device name cannot be empty.")

        token = self._require_token()
        metadata = self._fetch_metadata(token)
        current = self._extract_profile(metadata)
        machine_id = get_or_create_machine_id()

        index = next(
            (i for i, d in enumerate(current.devices) if d.id == machine_id),
            None,
        )
        if index is None:
            raise ProfileError(
                f"This machine (id={machine_id}) isn't registered yet. "
                "Run `profile device register` first.",
            )
        existing = current.devices[index]
        renamed = existing.model_copy(
            update={"name": new_name.strip(), "last_seen": _utc_now_iso()},
        )
        devices = [*current.devices]
        devices[index] = renamed
        updated = current.model_copy(update={"devices": devices})
        self._write_profile(token, metadata, updated)
        self._cache = updated
        return renamed

    def remove_device(self, device_id: str) -> bool:
        """Drop the entry for *device_id*. Returns True on removal."""
        token = self._require_token()
        metadata = self._fetch_metadata(token)
        current = self._extract_profile(metadata)
        remaining = [d for d in current.devices if d.id != device_id]
        if len(remaining) == len(current.devices):
            return False
        updated = current.model_copy(update={"devices": remaining})
        self._write_profile(token, metadata, updated)
        self._cache = updated
        return True

    def touch_device(self) -> DeviceInfo | None:
        """Update ``last_seen`` for the current machine if registered.

        Returns ``None`` when the current machine isn't registered.
        Lighter than :meth:`register_device` because it doesn't create
        a new entry.
        """
        token = self._require_token()
        metadata = self._fetch_metadata(token)
        current = self._extract_profile(metadata)
        machine_id = get_or_create_machine_id()

        index = next(
            (i for i, d in enumerate(current.devices) if d.id == machine_id),
            None,
        )
        if index is None:
            return None
        existing = current.devices[index]
        touched = existing.model_copy(update={"last_seen": _utc_now_iso()})
        devices = [*current.devices]
        devices[index] = touched
        updated = current.model_copy(update={"devices": devices})
        self._write_profile(token, metadata, updated)
        self._cache = updated
        return touched

    # ----------------------------------------------------------------------
    # Internals
    # ----------------------------------------------------------------------

    def _require_token(self) -> str:
        token = self.get_access_token()
        if not token:
            raise ProfileError(
                "No valid Supabase session. Run `obscura-auth login` first.",
            )
        return token

    def _fetch(self, token: str) -> ObscuraProfile:
        metadata = self._fetch_metadata(token)
        return self._extract_profile(metadata)

    def _fetch_metadata(self, token: str) -> dict[str, Any]:
        resp = httpx.get(
            f"{self.api_url}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": self.anon_key,
            },
            timeout=_REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            raise ProfileError(
                f"Supabase GET /auth/v1/user failed ({resp.status_code}): {resp.text}",
            )
        body = resp.json()
        user_metadata = body.get("user_metadata")
        if not isinstance(user_metadata, dict):
            return {}
        return cast("dict[str, Any]", user_metadata)

    def _extract_profile(self, metadata: dict[str, Any]) -> ObscuraProfile:
        raw = metadata.get(_METADATA_PROFILE_KEY)
        if not isinstance(raw, dict):
            return ObscuraProfile()
        try:
            return ObscuraProfile.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            # A corrupt or drifted profile field shouldn't brick startup.
            # We log loudly so an operator notices, but return an empty
            # profile rather than raise.
            logger.warning("Stored profile failed validation: %s", exc)
            return ObscuraProfile()

    def _write_profile(
        self,
        token: str,
        current_metadata: dict[str, Any],
        profile: ObscuraProfile,
    ) -> None:
        merged = {
            **current_metadata,
            _METADATA_PROFILE_KEY: profile.model_dump(mode="json"),
        }
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
            raise ProfileError(
                f"Supabase PUT /auth/v1/user failed ({resp.status_code}): {resp.text}",
            )


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------


_singleton_lock = threading.Lock()
_singleton: ProfileClient | None = None
_singleton_config: tuple[str, str] | None = None


def get_client() -> ProfileClient | None:
    """Return the process-wide profile client, or ``None`` when Supabase isn't configured."""
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
        _singleton = ProfileClient(
            api_url=cfg.url,
            anon_key=cfg.anon_key,
            get_access_token=get_access_token,
        )
        _singleton_config = config_tuple
        return _singleton


def reset() -> None:
    """Forget the cached client -- for tests."""
    global _singleton, _singleton_config

    with _singleton_lock:
        _singleton = None
        _singleton_config = None


__all__ = [
    "DeviceInfo",
    "ObscuraProfile",
    "ProfileClient",
    "ProfileError",
    "current_device_info",
    "get_client",
    "get_or_create_machine_id",
    "reset",
]
