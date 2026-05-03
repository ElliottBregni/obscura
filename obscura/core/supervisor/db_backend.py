"""obscura.core.supervisor.db_backend — Backend abstraction for supervisor storage.

Provides a thin protocol that normalizes SQLite and PostgreSQL connection
management + SQL dialect differences so the 7+ supervisor data-access
classes can work with either backend without rewriting their SQL.

Usage::

    # SQLite (default, backward-compatible)
    backend = SQLiteSupervisorBackend("/path/to/supervisor.db")

    # PostgreSQL (from shared pool)
    backend = PostgreSQLSupervisorBackend()

    # Factory (reads OBSCURA_DB_TYPE env var)
    backend = create_supervisor_backend(db_path="/path/to/supervisor.db")
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from obscura.core.pg_config import HAS_PSYCOPG2, PGPoolManager
from obscura.core.supervisor.postgres_schema import init_supervisor_schema_pg
from obscura.core.supervisor.schema import init_supervisor_schema

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class DatabaseBackend(Protocol):
    """Abstract backend for supervisor storage.

    Implementations provide connection management and SQL dialect
    translation.  All supervisor data-access classes accept this
    instead of a raw ``db_path``.
    """

    @property
    def dialect(self) -> str:
        """Return ``"sqlite"`` or ``"postgresql"``."""
        ...

    def get_conn(self) -> Any:
        """Acquire a database connection.

        For SQLite this returns a thread-local ``sqlite3.Connection``.
        For PostgreSQL this borrows from the shared pool.

        Callers **must** call :meth:`put_conn` when done (typically in
        a ``try/finally``).
        """
        ...

    def put_conn(self, conn: Any) -> None:
        """Release a connection back to the pool.

        No-op for SQLite (thread-local connections are long-lived).
        """
        ...

    def init_schema(self) -> None:
        """Run the DDL for this backend (idempotent)."""
        ...

    def close(self) -> None:
        """Release resources held by this backend."""
        ...


# ---------------------------------------------------------------------------
# SQL translation helpers
# ---------------------------------------------------------------------------


# Pre-compiled patterns for translate_sql
_PH_RE = re.compile(r"\?")
_INSERT_OR_IGNORE_RE = re.compile(
    r"INSERT\s+OR\s+IGNORE\s+INTO",
    re.IGNORECASE,
)
_INSERT_OR_REPLACE_RE = re.compile(
    r"INSERT\s+OR\s+REPLACE\s+INTO",
    re.IGNORECASE,
)
_CURRENT_TIMESTAMP_RE = re.compile(
    r"\bCURRENT_TIMESTAMP\b",
    re.IGNORECASE,
)
# Match AUTOINCREMENT (SQLite-specific)
_AUTOINCREMENT_RE = re.compile(r"\bAUTOINCREMENT\b", re.IGNORECASE)


def translate_sql(sql: str, dialect: str) -> str:
    """Mechanically translate SQLite SQL to PostgreSQL SQL.

    Handles:
    - ``?`` placeholders  → ``%s``
    - ``INSERT OR IGNORE`` → ``INSERT ... ON CONFLICT DO NOTHING``
    - ``INSERT OR REPLACE`` → basic ``INSERT ... ON CONFLICT DO UPDATE``
      (caller must ensure the table has a UNIQUE/PK constraint)
    - ``CURRENT_TIMESTAMP`` → ``NOW()``
    - ``AUTOINCREMENT`` → removed (PostgreSQL SERIAL handles this)

    Returns the SQL unchanged when *dialect* is ``"sqlite"``.
    """
    if dialect == "sqlite":
        return sql

    result = sql

    # Handle INSERT OR IGNORE → INSERT INTO ... ON CONFLICT DO NOTHING
    if _INSERT_OR_IGNORE_RE.search(result):
        result = _INSERT_OR_IGNORE_RE.sub("INSERT INTO", result)
        result = result.rstrip().rstrip(";")
        result += " ON CONFLICT DO NOTHING"
    # Handle INSERT OR REPLACE (callers should prefer explicit ON CONFLICT clauses)
    elif _INSERT_OR_REPLACE_RE.search(result):
        result = _INSERT_OR_REPLACE_RE.sub("INSERT INTO", result)

    # Placeholder conversion: ? → %s
    result = _PH_RE.sub("%s", result)

    # CURRENT_TIMESTAMP → NOW()
    result = _CURRENT_TIMESTAMP_RE.sub("NOW()", result)

    # Remove AUTOINCREMENT (PG SERIAL handles auto-increment)
    result = _AUTOINCREMENT_RE.sub("", result)

    return result


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------


class SQLiteSupervisorBackend:
    """SQLite backend — wraps the existing thread-local connection pattern.

    Drop-in replacement for the current raw ``sqlite3`` usage.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.init_schema()

    @property
    def dialect(self) -> str:
        return "sqlite"

    def get_conn(self) -> sqlite3.Connection:
        """Return a thread-local SQLite connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return self._local.conn

    def put_conn(self, conn: Any) -> None:  # noqa: ARG002
        """No-op — SQLite connections are thread-local and long-lived."""

    def init_schema(self) -> None:
        """Run the supervisor DDL (idempotent)."""
        init_supervisor_schema(self.get_conn())

    def close(self) -> None:
        """Close the thread-local connection."""
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None

    @property
    def db_path(self) -> Path:
        """Expose the path for components that need it (e.g. SessionLock compat)."""
        return self._db_path


# ---------------------------------------------------------------------------
# PostgreSQL backend
# ---------------------------------------------------------------------------


class PostgreSQLSupervisorBackend:
    """PostgreSQL backend — uses the shared ``PGPoolManager`` pool.

    Connection pool is shared with the event store, memory, and
    vector-memory stores to avoid pool exhaustion.
    """

    def __init__(self) -> None:
        if not HAS_PSYCOPG2:
            msg = (
                "psycopg2 is required for PostgreSQL support. "
                "Install it with: pip install psycopg2-binary"
            )
            raise ImportError(msg)

        self._pool = self._get_pool()
        self.init_schema()

    @staticmethod
    def _get_pool() -> Any:
        return PGPoolManager.get_pool()

    @property
    def dialect(self) -> str:
        return "postgresql"

    def get_conn(self) -> Any:
        """Borrow a connection from the shared pool."""
        conn = self._pool.getconn()
        conn.autocommit = False
        return conn

    def put_conn(self, conn: Any) -> None:
        """Return a connection to the pool."""
        self._pool.putconn(conn)

    def init_schema(self) -> None:
        """Run the PostgreSQL supervisor DDL (idempotent)."""
        conn = self.get_conn()
        try:
            init_supervisor_schema_pg(conn)
        finally:
            self.put_conn(conn)

    def close(self) -> None:
        """No-op — pool lifecycle is managed by ``PGPoolManager``."""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_supervisor_backend(
    db_path: str | Path | None = None,
) -> DatabaseBackend:
    """Create a supervisor backend based on configuration.

    When ``OBSCURA_DB_TYPE=postgresql`` and PG credentials are set,
    returns a :class:`PostgreSQLSupervisorBackend`.  Otherwise returns
    a :class:`SQLiteSupervisorBackend`.

    Args:
        db_path: Path for the SQLite database (used when the backend
            is SQLite).  Defaults to ``~/.obscura/supervisor.db``.

    """
    db_type = os.getenv("OBSCURA_DB_TYPE", "sqlite").lower()

    if db_type == "postgresql":
        try:
            return PostgreSQLSupervisorBackend()
        except (ImportError, ValueError) as exc:
            logger.warning(
                "PostgreSQL requested but unavailable (%s), falling back to SQLite",
                exc,
            )

    # SQLite fallback
    if db_path is None:
        db_path = Path.home() / ".obscura" / "supervisor.db"
    return SQLiteSupervisorBackend(db_path)
