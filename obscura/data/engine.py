"""Connection management for the data layer.

Resolves backend selection from env, exposes connection-acquisition
context managers for SQLite and Postgres. No SQLAlchemy — raw cursors,
hand-written SQL, organised by domain in subpackages.

Backend resolution (highest priority first):

1. ``OBSCURA_DB_URL`` — explicit URL (``postgresql://...`` or ``sqlite://...``)
2. ``OBSCURA_PG_HOST`` / ``OBSCURA_PG_PASSWORD`` — legacy per-var Postgres config
3. SQLite default (file under ``~/.obscura/<name>.db``)

Fail-loud on bad config: a malformed ``OBSCURA_DB_URL`` or a Postgres
selection without a reachable server raises ``DataLayerError`` at
connection time. Silent fallback would let prod misconfigurations hide.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from obscura.core.pg_config import PGPoolManager

logger = logging.getLogger(__name__)


class DataLayerError(RuntimeError):
    """Raised when the data layer can't establish a usable connection."""


class Backend(StrEnum):
    """The two relational backends the data layer supports."""

    SQLITE = "sqlite"
    POSTGRES = "postgres"


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------


def _parse_db_url(url: str) -> Backend:
    """Map a connection URL scheme to a Backend, or raise."""
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme in ("postgres", "postgresql") or scheme.startswith("postgresql+"):
        return Backend.POSTGRES
    if scheme in ("sqlite", "sqlite3"):
        return Backend.SQLITE
    msg = (
        f"Unsupported OBSCURA_DB_URL scheme: {scheme!r}. "
        "Use postgresql:// or sqlite://."
    )
    raise DataLayerError(msg)


def resolve_backend() -> Backend:
    """Pick the backend the data layer should use, based on env vars."""
    url = os.environ.get("OBSCURA_DB_URL", "").strip()
    if url:
        return _parse_db_url(url)
    if os.environ.get("OBSCURA_PG_HOST") or os.environ.get("OBSCURA_PG_PASSWORD"):
        return Backend.POSTGRES
    return Backend.SQLITE


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


_DEFAULT_SQLITE_DIR = Path.home() / ".obscura"


def sqlite_path(name: str) -> Path:
    """Return the on-disk path for a SQLite store, creating parent dirs.

    If ``OBSCURA_DB_URL=sqlite:///abs/path/<name>.db`` is set, the URL's
    path is honoured. Otherwise ``~/.obscura/<name>.db``.
    """
    url = os.environ.get("OBSCURA_DB_URL", "").strip()
    if url:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if scheme in ("sqlite", "sqlite3"):
            # sqlite:///abs/path → parsed.path = '/abs/path'
            # Treat the URL as the base directory; append <name>.db
            base = Path(parsed.path or str(_DEFAULT_SQLITE_DIR))
            base.mkdir(parents=True, exist_ok=True)
            return base / f"{name}.db"
    _DEFAULT_SQLITE_DIR.mkdir(parents=True, exist_ok=True)
    return _DEFAULT_SQLITE_DIR / f"{name}.db"


@contextlib.contextmanager
def sqlite_connection(name: str) -> Iterator[sqlite3.Connection]:
    """Open a SQLite connection for store *name* with row-dict access.

    Caller is responsible for committing transactions; on exit the
    connection is closed unconditionally (no implicit rollback — caller
    rolls back on its own errors).
    """
    path = sqlite_path(name)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            logger.debug("error closing sqlite conn for %s", name, exc_info=True)


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


def _apply_url_to_pg_env(url: str) -> None:
    """Populate ``OBSCURA_PG_*`` from a Postgres URL when set.

    Lets us reuse :class:`obscura.core.pg_config.PGPoolManager` without
    duplicating its psycopg2 setup. URL fields override env if present;
    missing fields leave env unchanged so existing values still apply.
    """
    parsed = urlparse(url)
    if (parsed.scheme or "").lower() not in (
        "postgres",
        "postgresql",
    ) and not (parsed.scheme or "").lower().startswith("postgresql+"):
        return
    if parsed.hostname:
        os.environ["OBSCURA_PG_HOST"] = parsed.hostname
    if parsed.port:
        os.environ["OBSCURA_PG_PORT"] = str(parsed.port)
    if parsed.username:
        os.environ["OBSCURA_PG_USER"] = parsed.username
    if parsed.password:
        os.environ["OBSCURA_PG_PASSWORD"] = parsed.password
    if parsed.path and parsed.path != "/":
        os.environ["OBSCURA_PG_DATABASE"] = parsed.path.lstrip("/")


def get_postgres_pool() -> Any:  # noqa: ANN401  # psycopg2 pool isn't typed
    """Return the shared Postgres connection pool, raising on bad config.

    Honours ``OBSCURA_DB_URL`` (Postgres scheme) by mapping it onto the
    legacy ``OBSCURA_PG_*`` env vars, then delegates to
    :class:`obscura.core.pg_config.PGPoolManager`. Wraps the underlying
    ``ValueError``/``ImportError`` in :class:`DataLayerError` so callers
    can catch one type.
    """
    url = os.environ.get("OBSCURA_DB_URL", "").strip()
    if url:
        _apply_url_to_pg_env(url)
    try:
        return PGPoolManager.get_pool()
    except (ImportError, ValueError) as exc:
        msg = f"Postgres pool unavailable: {exc}"
        raise DataLayerError(msg) from exc


@contextlib.contextmanager
def postgres_connection() -> Iterator[Any]:
    """Check out a Postgres connection from the pool, return it on exit."""
    pool = get_postgres_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        try:
            pool.putconn(conn)
        except Exception:
            logger.debug("error returning conn to pool", exc_info=True)
