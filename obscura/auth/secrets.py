"""obscura.auth.secrets -- Layered secret resolution for service config.

Resolution order (highest priority first):

1. **Shell env** -- values the user explicitly exported in the shell (or
   Docker/CI passed via ``-e``) before this process started. Snapshotted
   at module import, before any ``.env`` loading, so local repo config
   can't shadow what the operator deliberately set.
2. **OS keyring** -- macOS Keychain, Windows Credential Manager, or the
   freedesktop Secret Service on Linux desktops. Skipped transparently
   when no backend is available (headless Linux, Docker, CI).
3. **Supabase user vault** -- encrypted per-user bag in
   ``user_metadata.obscura_vault``, decrypted locally with a
   passphrase-derived Fernet key cached in the OS keyring. Opt-in and
   silent when the vault is locked or the user isn't signed in.
4. **dotenv** -- anything loaded from ``.env`` files (CWD,
   ``~/.obscura/.env``, or the installed package root). These sit
   *below* keyring and the cloud vault so an unrelated repo's ``.env``
   can't override a key the user already stored in either of those.
5. **Caller-provided default** -- used for non-secret values that may
   ship with public defaults (e.g. a project's public anon key).

This gives one API that does the right thing everywhere:

* On a developer's Mac, secrets live in the login keychain. They win
  over any repo-local ``.env`` the developer might ``cd`` into.
* Signing into Supabase on a fresh machine pulls from the cloud vault
  on demand -- keys follow the user, not the device.
* In Docker / Kubernetes / CI, operators pass env vars and the keyring
  + cloud vault calls are no-ops.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)

# Service name used when storing credentials in the OS keyring. Matches the
# naming used by ``obscura-auth login`` for session tokens so that secrets
# cluster under the same service entry in Keychain / Credential Manager.
_KEYRING_SERVICE = "obscura-cli"

# Hard cap on the value bytes we'll accept in ``store()``. Picked to catch
# obvious mistakes (dumping a PEM file or JSON blob into a keyring slot)
# while still comfortably fitting any sane API key, JWT, or refresh token.
_MAX_VALUE_BYTES = 64 * 1024

# ---------------------------------------------------------------------------
# Shell-env snapshot
# ---------------------------------------------------------------------------
# Captured at module import, before any ``.env`` loading can mutate
# ``os.environ``. This is the "what the operator actually exported in
# their shell" tier of the resolver. Tests patch this directly via
# ``monkeypatch.setattr(secrets, "_shell_env_snapshot", {...})``.
_shell_env_snapshot: dict[str, str] = dict(os.environ)


def _is_shell_env(name: str) -> bool:
    """True when *name* was present in the shell env at module import."""
    return bool(_shell_env_snapshot.get(name, "").strip())


# All Supabase config values the resolver knows about. Listed here so the
# CLI ``obscura-auth secrets list`` command can enumerate them without
# hard-coding the set in two places.
SUPABASE_SECRET_NAMES: tuple[str, ...] = (
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "SUPABASE_JWT_SECRET",
    "SUPABASE_JWKS_URL",
    "SUPABASE_AUDIENCE",
    "SUPABASE_ISSUER",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_ACCESS_TOKEN",  # Personal access token for the Supabase MCP HTTP server.
)

# LLM backend credentials. These names mirror the env-var tuples in
# :mod:`obscura.core.auth` so anything the backends can read from the env
# can also live in the OS keyring.
BACKEND_SECRET_NAMES: tuple[str, ...] = (
    # Copilot / GitHub
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "COPILOT_GITHUB_TOKEN",
    # Anthropic / Claude
    "ANTHROPIC_API_KEY",
    "CLAUDE_API_KEY",
    "CLAUDE_CODE_API_KEY",
    # OpenAI / Codex
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    # Moonshot / Kimi
    "MOONSHOT_API_KEY",
    "KIMI_API_KEY",
)

# Plugin / integration credentials. The backends don't consult these
# directly -- plugin loaders read ``os.environ`` themselves -- but storing
# them here still gives the user a single audit surface via
# ``/secrets list`` and lets ``obscura-auth secrets export`` replay them
# into a shell session when needed.
PLUGIN_SECRET_NAMES: tuple[str, ...] = (
    "QDRANT_API_KEY",
    "DD_API_KEY",
    "DD_APP_KEY",
    "NOTION_TOKEN",
    "X_BEARER_TOKEN",
    "X_ACCESS_TOKEN",
    "SHODAN_API_KEY",
    "ALPHAVANTAGE_API_KEY",
    "COINGECKO_API_KEY",
    "GRAFANA_API_KEY",
    "FLIGHTAWARE_API_KEY",
    "MARINETRAFFIC_API_KEY",
    "HF_TOKEN",
    "POLYGON_API_KEY",
    "SECURITYTRAILS_API_KEY",
    "CENSYS_API_ID",
    "CENSYS_API_SECRET",
    "AZURE_CLIENT_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
)

# All names the CLI will accept without ``--force``. The tuple order is the
# order ``secrets list`` will print them in, so keep Supabase first (it's
# the most common case for new users).
KNOWN_SECRET_NAMES: tuple[str, ...] = (
    *SUPABASE_SECRET_NAMES,
    *BACKEND_SECRET_NAMES,
    *PLUGIN_SECRET_NAMES,
)

# Values that should be masked when printed. Everything secret-ish is in
# here; only the handful of public values (URLs, audience strings, client
# IDs) get shown in full by ``secrets get`` without ``--reveal``.
SENSITIVE_SECRET_NAMES: frozenset[str] = frozenset(
    name
    for name in KNOWN_SECRET_NAMES
    if name
    not in {
        "SUPABASE_URL",
        "SUPABASE_JWKS_URL",
        "SUPABASE_AUDIENCE",
        "SUPABASE_ISSUER",
    }
)


class SecretsValidationError(ValueError):
    """Raised when a value fails pre-flight validation in :func:`store`.

    Distinct from keyring backend errors -- this is a caller-side problem
    (null byte in the value, oversized blob) that no retry will fix.
    """


_dotenv_loaded = False

# In-process resolution cache. Each name is queried through the full
# tier walk at most once per process lifetime. The big payoff is on
# macOS, where every keyring read is an IPC to securityd that may pop
# an authorization dialog -- without this cache, ~30 BACKEND/PLUGIN
# names get walked on every startup and again on every config refresh.
# Invalidated by ``store()`` / ``delete()`` and by ``clear_cache()``.
# ``None`` is a valid cached value meaning "we already looked and it
# wasn't anywhere," so we use a sentinel to distinguish "missing from
# cache" from "cached as unset."
_RESOLVE_MISS = object()
_resolve_cache: dict[str, str | None] = {}
_resolve_cache_lock = threading.Lock()


def clear_cache(name: str | None = None) -> None:
    """Drop cached resolutions so the next ``resolve()`` walks the tiers again.

    Pass *name* to invalidate a single entry; omit to clear the entire
    cache. Used by ``store()`` / ``delete()`` and exposed for tests and
    administrative tools that mutate secrets out-of-band.
    """
    with _resolve_cache_lock:
        if name is None:
            _resolve_cache.clear()
        else:
            _resolve_cache.pop(name, None)


def _load_dotenv_once() -> None:
    """Load ``.env`` files into the process environment exactly once.

    Searches three locations in order, and never overrides a value already
    present in the environment. Silently no-ops when ``python-dotenv``
    isn't installed -- the rest of the resolver still works via plain env
    vars.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True

    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    # 1. CWD -- standard dotenv behaviour.
    load_dotenv(override=False)
    # 2. User home -- survives any working directory.
    obscura_home = Path(os.environ.get("OBSCURA_HOME", Path.home() / ".obscura"))
    load_dotenv(obscura_home / ".env", override=False)
    # 3. Installed package root -- covers editable installs / dev loops.
    pkg_root = Path(__file__).resolve().parents[2]
    load_dotenv(pkg_root / ".env", override=False)


def keyring_available() -> bool:
    """Return True when the OS keyring backend can actually persist values.

    On headless Linux and inside Docker this returns False -- the keyring
    package reports a ``NullKeyring`` / ``FailKeyring`` backend which
    would silently drop writes. The broad except is intentional: any
    import or backend init failure means "no keyring," full stop.
    """
    try:
        import keyring

        backend = keyring.get_keyring()
    except Exception:
        return False
    return type(backend).__name__ not in {"NullKeyring", "FailKeyring"}


def _keyring_lookup(name: str) -> str | None:
    """Return the keyring-stored value for *name*, or None.

    Callers must have already verified :func:`keyring_available`.
    """
    import keyring
    import keyring.errors

    try:
        stored = keyring.get_password(_KEYRING_SERVICE, name)
    except keyring.errors.KeyringError as exc:
        logger.debug("Keyring read for %s failed: %s", name, exc)
        return None
    if stored and stored.strip():
        return stored.strip()
    return None


def resolve(name: str, *, default: str | None = None) -> str | None:
    """Resolve *name* via shell env, keyring, cloud vault, dotenv, default.

    Returns the resolved value stripped of surrounding whitespace, or
    ``default`` when nothing is configured. Empty strings in every tier
    are treated as unset so callers don't have to distinguish ``""``
    from ``None``.

    Results are cached for the lifetime of the process so the same name
    isn't re-queried through keyring / cloud vault on every call. Use
    :func:`clear_cache` after mutating secrets out-of-band.
    """
    cached = _resolve_cache.get(name, _RESOLVE_MISS)
    if cached is not _RESOLVE_MISS:
        # cached is either str or None at this point; _RESOLVE_MISS sentinel
        # is the only object() instance and we just excluded it.
        cached_str = cast("str | None", cached)
        return cached_str if cached_str is not None else default

    value: str | None = None

    # Tier 1 -- shell env (captured at module import, before any dotenv).
    shell_value = _shell_env_snapshot.get(name, "").strip()
    if shell_value:
        value = shell_value

    # Tier 2 -- OS keyring.
    if value is None and keyring_available():
        kr_value = _keyring_lookup(name)
        if kr_value:
            value = kr_value

    # Tier 3 -- Supabase encrypted vault. Silent if locked/absent.
    if value is None:
        vault_value = _vault_lookup(name)
        if vault_value:
            value = vault_value

    # Tier 4 -- dotenv-loaded values (arrive in ``os.environ`` below).
    if value is None:
        _load_dotenv_once()
        env_value = os.environ.get(name, "").strip()
        if env_value:
            value = env_value

    with _resolve_cache_lock:
        _resolve_cache[name] = value

    return value if value is not None else default


# Re-entry guard: ``get_client()`` itself calls ``SupabaseCliConfig.from_env()``
# which calls ``resolve("SUPABASE_URL")``. Without this guard, the vault
# tier would call back into ``resolve`` for its own bootstrap config and
# recurse forever. Keyed on threading.local so concurrent resolves in
# async callers don't block each other.
_vault_reentry = threading.local()

# Names the vault tier never looks up -- they're bootstrap config that
# the vault client itself needs to connect. Any attempt to resolve these
# via the vault would recurse through ``get_client`` → ``from_env`` →
# ``resolve`` → here.
_VAULT_BOOTSTRAP_SKIP: frozenset[str] = frozenset(
    {"SUPABASE_URL", "SUPABASE_ANON_KEY"},
)


def _vault_lookup(name: str) -> str | None:
    """Fetch *name* from the Supabase cloud vault, or ``None``.

    Isolated so the resolver stays readable and tests can stub the
    cloud tier independently of keyring / env. Any error -- no
    session, vault locked, network failure -- becomes a silent
    ``None`` so the resolver falls through to the next tier.
    """
    if name in _VAULT_BOOTSTRAP_SKIP:
        return None
    if getattr(_vault_reentry, "active", False):
        return None
    _vault_reentry.active = True
    try:
        from obscura.auth import supabase_secrets as _vault
    except Exception as exc:  # pragma: no cover -- circular-import defence
        logger.debug("Vault module unavailable: %s", exc)
        _vault_reentry.active = False
        return None

    try:
        client = _vault.get_client()
        if client is None:
            return None
        try:
            value = client.get(name)
        except Exception as exc:  # noqa: BLE001 -- resolver tier is total
            logger.debug("Vault lookup for %s failed: %s", name, exc)
            return None
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None
    finally:
        _vault_reentry.active = False


def store(name: str, value: str) -> bool:
    """Persist *value* under *name* in the OS keyring.

    Returns ``True`` when the write succeeds, ``False`` when no keyring
    backend is available. Raises :class:`SecretsValidationError` when the
    value itself is malformed (contains NUL bytes or exceeds
    :data:`_MAX_VALUE_BYTES`) -- those failures won't improve on retry
    and should be surfaced, not silently swallowed.
    """
    _validate_value(name, value)

    if not keyring_available():
        return False
    try:
        import keyring
        import keyring.errors
    except ImportError as exc:
        logger.debug("Keyring package missing during store: %s", exc)
        return False

    try:
        keyring.set_password(_KEYRING_SERVICE, name, value)
    except keyring.errors.KeyringError as exc:
        logger.warning("Keyring write for %s failed: %s", name, exc)
        return False
    clear_cache(name)
    return True


def _validate_value(name: str, value: str) -> None:
    """Reject values that no keyring backend should ever be asked to store.

    NUL bytes truncate on some Linux Secret Service backends and outright
    fail on Windows DPAPI. Oversized values almost always mean the caller
    pasted the wrong thing (a PEM file, a JSON blob) and will bite later
    when something reads them back.
    """
    if "\x00" in value:
        raise SecretsValidationError(
            f"Refusing to store {name}: value contains NUL bytes, which "
            "some keyring backends truncate or reject.",
        )
    size = len(value.encode("utf-8", errors="replace"))
    if size > _MAX_VALUE_BYTES:
        raise SecretsValidationError(
            f"Refusing to store {name}: value is {size} bytes, exceeds the "
            f"{_MAX_VALUE_BYTES}-byte limit. If you genuinely need this, "
            "keep it on disk and pass the path through an env var instead.",
        )


def delete(name: str) -> bool:
    """Remove *name* from the keyring if present.

    Returns ``True`` when a value was actually removed. A ``False`` return
    means "nothing was stored under that name" -- not an error.
    """
    if not keyring_available():
        return False
    try:
        import keyring
        import keyring.errors
    except ImportError as exc:
        logger.debug("Keyring package missing during delete: %s", exc)
        return False

    try:
        keyring.delete_password(_KEYRING_SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        return False
    except keyring.errors.KeyringError as exc:
        logger.debug("Keyring delete for %s failed: %s", name, exc)
        return False
    clear_cache(name)
    return True


def sources(names: Iterable[str] = KNOWN_SECRET_NAMES) -> dict[str, str]:
    """Report where each *name* is currently sourced from.

    Returns a mapping ``{name: source}`` where ``source`` is one of:

    * ``"shell"`` -- set by the operator's shell (or Docker ``-e``)
      before this process started.
    * ``"keyring"`` -- stored in the OS keyring.
    * ``"supabase"`` -- decrypted from the user's cloud vault.
    * ``"dotenv"`` -- loaded from a ``.env`` file. Lower precedence than
      keyring and the vault at resolution time.
    * ``"missing"`` -- not configured anywhere.

    Used by the ``obscura-auth secrets list`` CLI command to give the
    user a single-pane view of their config without exposing the values.
    """
    _load_dotenv_once()

    result: dict[str, str] = {}
    kr_ready = keyring_available()
    vault_snapshot = _vault_snapshot()

    for name in names:
        if _is_shell_env(name):
            result[name] = "shell"
            continue
        if kr_ready and _keyring_lookup(name):
            result[name] = "keyring"
            continue
        if name in vault_snapshot and vault_snapshot[name].strip():
            result[name] = "supabase"
            continue
        if os.environ.get(name, "").strip():
            # Not in the shell snapshot and not in keyring or vault, so
            # whatever is in ``os.environ`` now must have come from
            # dotenv loading.
            result[name] = "dotenv"
            continue
        result[name] = "missing"

    return result


def _vault_snapshot() -> dict[str, str]:
    """Return the decrypted vault as a dict, or {} on any failure.

    Used by :func:`sources` and :func:`materialize_to_environ` which
    both need the whole bag rather than looking up one name at a time.
    """
    if getattr(_vault_reentry, "active", False):
        return {}
    _vault_reentry.active = True
    try:
        try:
            from obscura.auth import supabase_secrets as _vault
        except Exception as exc:  # pragma: no cover
            logger.debug("Vault module unavailable: %s", exc)
            return {}

        client = _vault.get_client()
        if client is None:
            return {}
        try:
            return client.snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Vault snapshot failed: %s", exc)
            return {}
    finally:
        _vault_reentry.active = False


def materialize_to_environ(
    names: Iterable[str] = KNOWN_SECRET_NAMES,
) -> list[str]:
    """Copy keyring + cloud-vault secrets into ``os.environ``.

    Third-party plugin loaders read ``os.environ`` directly, so a secret
    stored only in the OS keyring or the Supabase vault is invisible to
    them until it's materialised. Precedence is preserved end-to-end:
    shell env > keyring > cloud vault > dotenv. Values the operator set
    in their shell (captured in the snapshot at import) are never
    touched; dotenv values get overwritten by the higher tiers.

    Returns the list of names actually written.
    """
    copied: list[str] = []

    # Keyring tier. ``keyring_available()`` already guards the import,
    # so we can call ``_keyring_lookup`` directly without a second probe.
    if keyring_available():
        for name in names:
            if _is_shell_env(name):
                continue
            kr_value = _keyring_lookup(name)
            if kr_value is None:
                continue
            os.environ[name] = kr_value
            _materialized_names.add(name)
            copied.append(name)

    # Cloud vault tier -- only fills names keyring didn't already handle.
    vault_snapshot = _vault_snapshot()
    for name, value in vault_snapshot.items():
        if _is_shell_env(name):
            continue
        if name in _materialized_names:
            # Keyring already wrote a value for this name above.
            continue
        if not value.strip():
            continue
        os.environ[name] = value.strip()
        _materialized_names.add(name)
        copied.append(name)

    return copied


# ---------------------------------------------------------------------------
# Subprocess env filtering (opt-in paranoid mode)
# ---------------------------------------------------------------------------
# By default Obscura tools spawn subprocesses with the parent ``os.environ``
# inherited in full -- standard POSIX behaviour. When
# ``OBSCURA_TOOL_ENV_STRICT=1``, :func:`safe_subprocess_env` strips every
# name in :data:`KNOWN_SECRET_NAMES` plus anything :func:`materialize_to_environ`
# wrote into ``os.environ`` before handing the env to a subprocess. This
# prevents a prompted LLM from shelling out to ``printenv`` (or reading
# ``os.environ`` from a Python child) to exfiltrate keys.

_STRICT_MODE_ENV_VAR = "OBSCURA_TOOL_ENV_STRICT"
_AUDIT_LOG_ENV_VAR = "OBSCURA_SECRETS_AUDIT_LOG"

# Populated by :func:`materialize_to_environ` so ``--force``'d secrets the
# user set via ``/secrets set MY_CUSTOM_KEY foo --force`` also get stripped
# in strict mode. A plain set is fine -- nothing uses this concurrently
# outside the CLI bootstrap (single-threaded).
_materialized_names: set[str] = set()


def _strict_mode() -> bool:
    """True when the operator has opted into paranoid subprocess filtering."""
    raw = os.environ.get(_STRICT_MODE_ENV_VAR, "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def audit_log_path() -> Path:
    """Path to the JSONL audit log. Override via ``OBSCURA_SECRETS_AUDIT_LOG``.

    Defaults to ``$OBSCURA_HOME/logs/secrets-audit.jsonl`` which follows
    the existing Kairos log convention. Exposed as a public helper so the
    CLI ``secrets strict-env`` command can print the path and tail the
    file without duplicating the resolution logic.
    """
    override = os.environ.get(_AUDIT_LOG_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    home = Path(os.environ.get("OBSCURA_HOME", str(Path.home() / ".obscura")))
    return home / "logs" / "secrets-audit.jsonl"


def _append_audit(event: dict[str, object]) -> None:
    """Best-effort append of a single JSON line to the audit log.

    Any I/O failure is swallowed -- audit is observability, not a
    correctness constraint, and a failed write must never prevent a
    subprocess from launching.
    """
    try:
        path = audit_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
    except OSError as exc:
        logger.debug("Audit log write failed: %s", exc)


def safe_subprocess_env(
    extras: Mapping[str, str] | None = None,
    *,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an env dict safe to hand to a subprocess.

    Default behaviour mirrors ``os.environ`` inheritance: returns a copy
    of ``base`` (or ``os.environ`` when ``base`` is ``None``) with
    ``extras`` merged on top. No filtering is applied -- callers keep
    today's semantics unless they opt in.

    When ``OBSCURA_TOOL_ENV_STRICT=1`` is set, every name in
    :data:`KNOWN_SECRET_NAMES` plus anything in :data:`_materialized_names`
    is stripped from the result. ``extras`` are layered on *after* the
    strip, so callers can explicitly opt a specific secret back in for
    a tool that genuinely needs it (e.g. an MCP server that consumes
    ``ANTHROPIC_API_KEY``).

    Parameters
    ----------
    extras:
        Caller-supplied env values that always flow through regardless
        of strict mode. This is the escape hatch.
    base:
        Override the source env dict. Defaults to ``os.environ``. Tests
        pass a synthetic base here to assert behaviour without mutating
        the process env.
    """
    source = dict(base) if base is not None else dict(os.environ)
    if _strict_mode():
        stripped: list[str] = []
        for name in set(KNOWN_SECRET_NAMES) | _materialized_names:
            if name in source:
                stripped.append(name)
                del source[name]
        if stripped:
            _append_audit(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "event": "strict_strip",
                    "stripped": sorted(stripped),
                    "count": len(stripped),
                },
            )
    if extras:
        source.update(extras)
    return source


def mask(value: str | None) -> str:
    """Return a redacted preview of *value* for CLI output.

    Shows the last four characters so the user can tell which key is stored
    without exposing it in full -- mirrors the pattern used by ``aws sts``
    and ``gh auth status``.
    """
    if not value:
        return "(unset)"
    if len(value) <= 8:
        return "***"
    return f"***{value[-4:]}"


# Public aliases for cross-module use. The underscore-prefixed internals
# stay to avoid breaking code that imports them, but new callers (e.g.
# obscura.auth.supabase_secrets) should prefer the public names.
KEYRING_SERVICE = _KEYRING_SERVICE
append_audit = _append_audit


__all__ = [
    "BACKEND_SECRET_NAMES",
    "KEYRING_SERVICE",
    "KNOWN_SECRET_NAMES",
    "PLUGIN_SECRET_NAMES",
    "SENSITIVE_SECRET_NAMES",
    "SUPABASE_SECRET_NAMES",
    "SecretsValidationError",
    "append_audit",
    "audit_log_path",
    "delete",
    "keyring_available",
    "mask",
    "materialize_to_environ",
    "resolve",
    "safe_subprocess_env",
    "sources",
    "store",
]
