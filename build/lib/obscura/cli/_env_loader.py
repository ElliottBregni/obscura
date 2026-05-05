"""obscura.cli._env_loader — Startup environment and secrets materialization.

Extracted from cli/__init__.py. Called once at REPL startup before any
auth or plugin code runs.
"""

from __future__ import annotations

import logging

_log = logging.getLogger("obscura.cli")


def load_dotenvs() -> None:
    """Load .env files in priority order (global → project-local → CWD).

    Priority:  shell env > global ~/.obscura/.env > project .obscura/.env > CWD .env

    ``load_dotenv(override=False)`` never overwrites already-set vars, so
    calling order defines priority.
    """
    try:
        from dotenv import load_dotenv

        from obscura.core.paths import resolve_obscura_global_home, resolve_obscura_home

        # 1. Global ~/.obscura/.env  (user-wide creds/keys)
        global_env = resolve_obscura_global_home() / ".env"
        if global_env.is_file():
            load_dotenv(global_env)

        # 2. Project-local .obscura/.env  (only if different from global)
        local_env = resolve_obscura_home() / ".env"
        if local_env.is_file() and local_env.resolve() != global_env.resolve():
            load_dotenv(local_env)

        # 3. CWD .env — won't overwrite already-set vars
        load_dotenv()

    except Exception as exc:
        _log.debug("dotenv load failed: %s", exc)


def materialize_secrets() -> None:
    """Push OS keyring entries into os.environ.

    Plugins that read env vars directly can then see keys stored via
    ``/secrets set``.  Never overwrites values already present in the
    environment, so shell env and .env files always win.
    """
    try:
        from obscura.auth.secrets import materialize_to_environ

        materialize_to_environ()
    except Exception as exc:
        _log.debug("Secret materialization failed: %s", exc)


def bootstrap_env() -> None:
    """Run all startup env steps in order: dotenv, then secrets."""
    load_dotenvs()
    materialize_secrets()
