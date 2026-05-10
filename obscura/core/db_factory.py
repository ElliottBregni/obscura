"""Database factory for switching between SQLite and PostgreSQL event stores.

Provides backwards-compatible database initialization based on environment configuration.
"""

from __future__ import annotations

import logging
import os

from obscura.core.event_store import EventStoreProtocol, SQLiteEventStore
from obscura.core.paths import resolve_obscura_home
from obscura.core.postgres_event_store import PostgreSQLEventStore

# Public alias — callers should type against the Protocol, not the union.
EventStore = EventStoreProtocol
logger = logging.getLogger(__name__)


class DatabaseFactory:
    """Factory for creating event store instances based on configuration."""

    @staticmethod
    def create_event_store(db_type: str | None = None) -> EventStoreProtocol:
        """Create an event store instance.

        Args:
            db_type: Database type ('sqlite' or 'postgresql').
                    If None, uses OBSCURA_DB_TYPE environment variable.
                    Defaults to 'sqlite' for backwards compatibility.

        Returns:
            Event store instance (SQLiteEventStore or PostgreSQLEventStore)

        Environment Variables:
            OBSCURA_DB_TYPE: 'sqlite' or 'postgresql' (default: sqlite)

            For PostgreSQL:
            OBSCURA_PG_HOST: PostgreSQL host (default: localhost)
            OBSCURA_PG_PORT: PostgreSQL port (default: 5432)
            OBSCURA_PG_DATABASE: Database name (default: obscura)
            OBSCURA_PG_USER: Database user (default: obscura_user)
            OBSCURA_PG_PASSWORD: Database password (required for PostgreSQL)
            OBSCURA_PG_MIN_CONNECTIONS: Minimum pool connections (default: 2)
            OBSCURA_PG_MAX_CONNECTIONS: Maximum pool connections (default: 10)

        """
        if db_type is None:
            db_type = os.getenv("OBSCURA_DB_TYPE", "sqlite").lower()

        if db_type == "postgresql":
            return DatabaseFactory._create_postgres_store()
        if db_type == "sqlite":
            return DatabaseFactory._create_sqlite_store()
        msg = f"Unknown database type: {db_type}. Use 'sqlite' or 'postgresql'"
        raise ValueError(msg)

    @staticmethod
    def _create_sqlite_store() -> SQLiteEventStore:
        """Create SQLite event store with default configuration."""
        db_path = resolve_obscura_home() / "events.db"
        logger.info("DatabaseFactory: sqlite event store path=%s", db_path)
        return SQLiteEventStore(db_path=db_path)

    @staticmethod
    def _create_postgres_store() -> PostgreSQLEventStore:
        """Create PostgreSQL event store from environment configuration."""
        password = os.getenv("OBSCURA_PG_PASSWORD")
        if not password:
            msg = (
                "OBSCURA_PG_PASSWORD environment variable is required for PostgreSQL. "
                "Set it to your database password."
            )
            raise ValueError(msg)

        return PostgreSQLEventStore(
            host=os.getenv("OBSCURA_PG_HOST", "localhost"),
            port=int(os.getenv("OBSCURA_PG_PORT", "5432")),
            database=os.getenv("OBSCURA_PG_DATABASE", "obscura"),
            user=os.getenv("OBSCURA_PG_USER", "obscura_user"),
            password=password,
            min_connections=int(os.getenv("OBSCURA_PG_MIN_CONNECTIONS", "2")),
            max_connections=int(os.getenv("OBSCURA_PG_MAX_CONNECTIONS", "10")),
        )


# Convenience function for backwards compatibility
def get_event_store(db_type: str | None = None) -> EventStoreProtocol:
    """Get an event store instance.

    This is a convenience wrapper around DatabaseFactory.create_event_store().

    Args:
        db_type: Database type ('sqlite' or 'postgresql').
                If None, uses OBSCURA_DB_TYPE environment variable.

    Returns:
        Event store instance

    """
    return DatabaseFactory.create_event_store(db_type)
