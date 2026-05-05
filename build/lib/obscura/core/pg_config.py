"""Shared PostgreSQL configuration for all Obscura stores.

Reads connection parameters from OBSCURA_PG_* environment variables and
provides a singleton connection pool that can be shared across the
supervisor, memory, and vector-memory stores.

When OBSCURA_DB_TYPE is set to "postgresql", all stores that support
PostgreSQL will use it.  Otherwise SQLite is used (the default).
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any
import logging

logger = logging.getLogger(__name__)


_psycopg2: Any
_RealDictCursor: Any
try:
    import psycopg2
    import psycopg2.pool  # noqa: F401  # pyright: ignore[reportUnusedImport]
    from psycopg2.extras import RealDictCursor

    _has_psycopg2 = True
    _psycopg2 = psycopg2
    _RealDictCursor = RealDictCursor
except ImportError:
    logger.debug("suppressed exception in <module>", exc_info=True)
    _has_psycopg2 = False
    _psycopg2 = None
    _RealDictCursor = None

HAS_PSYCOPG2 = _has_psycopg2


@dataclass(frozen=True)
class PGConfig:
    """PostgreSQL connection parameters."""

    host: str = "localhost"
    port: int = 5432
    database: str = "obscura"
    user: str = "obscura_user"
    password: str = ""
    min_connections: int = 2
    max_connections: int = 10

    @classmethod
    def from_env(cls) -> PGConfig:
        """Read configuration from OBSCURA_PG_* environment variables."""
        return cls(
            host=os.getenv("OBSCURA_PG_HOST", "localhost"),
            port=int(os.getenv("OBSCURA_PG_PORT", "5432")),
            database=os.getenv("OBSCURA_PG_DATABASE", "obscura"),
            user=os.getenv("OBSCURA_PG_USER", "obscura_user"),
            password=os.getenv("OBSCURA_PG_PASSWORD", ""),
            min_connections=int(os.getenv("OBSCURA_PG_MIN_CONNECTIONS", "2")),
            max_connections=int(os.getenv("OBSCURA_PG_MAX_CONNECTIONS", "10")),
        )


def is_pg_configured() -> bool:
    """Check whether PostgreSQL is the configured database backend."""
    return os.getenv("OBSCURA_DB_TYPE", "sqlite").lower() == "postgresql"


class PGPoolManager:
    """Singleton connection-pool manager shared across all stores.

    Usage::

        pool = PGPoolManager.get_pool()
        conn = pool.getconn()
        try:
            ...
        finally:
            pool.putconn(conn)
    """

    _pool: Any | None = None
    _lock = threading.Lock()
    _config: PGConfig | None = None

    @classmethod
    def get_pool(cls, config: PGConfig | None = None) -> Any:
        """Return the shared ``ThreadedConnectionPool`` (creating it on first call).

        Args:
            config: Optional explicit config.  If *None*, reads from env.

        Raises:
            ImportError: If ``psycopg2`` is not installed.
            ValueError: If ``OBSCURA_PG_PASSWORD`` is not set.

        """
        if cls._pool is not None:
            return cls._pool

        with cls._lock:
            # Double-check after acquiring the lock.
            if cls._pool is not None:
                return cls._pool

            if not HAS_PSYCOPG2:
                msg = (
                    "psycopg2 is required for PostgreSQL support. "
                    "Install it with: pip install psycopg2-binary"
                )
                raise ImportError(msg)

            cfg = config or PGConfig.from_env()
            if not cfg.password:
                msg = (
                    "OBSCURA_PG_PASSWORD environment variable is required "
                    "for PostgreSQL.  Set it to your database password."
                )
                raise ValueError(msg)

            cls._config = cfg
            cls._pool = _psycopg2.pool.ThreadedConnectionPool(
                cfg.min_connections,
                cfg.max_connections,
                host=cfg.host,
                port=cfg.port,
                database=cfg.database,
                user=cfg.user,
                password=cfg.password,
                cursor_factory=_RealDictCursor,
            )
            return cls._pool

    @classmethod
    def close(cls) -> None:
        """Close the shared pool (idempotent)."""
        with cls._lock:
            if cls._pool is not None:
                cls._pool.closeall()
                cls._pool = None
                cls._config = None

    @classmethod
    def reset(cls) -> None:
        """Reset the pool.  Alias for :meth:`close` (used in tests)."""
        cls.close()
