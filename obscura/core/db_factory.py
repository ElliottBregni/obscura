"""Database factory for switching between SQLite and PostgreSQL event stores.

Provides backwards-compatible database initialization based on environment configuration.
"""
import os
from typing import Optional

from obscura.core.event_store import SQLiteEventStore
from obscura.core.postgres_event_store import PostgreSQLEventStore


class DatabaseFactory:
    """Factory for creating event store instances based on configuration."""

    @staticmethod
    def create_event_store(db_type: Optional[str] = None):
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
            db_type = os.getenv('OBSCURA_DB_TYPE', 'sqlite').lower()

        if db_type == 'postgresql':
            return DatabaseFactory._create_postgres_store()
        elif db_type == 'sqlite':
            return DatabaseFactory._create_sqlite_store()
        else:
            raise ValueError(f"Unknown database type: {db_type}. Use 'sqlite' or 'postgresql'")

    @staticmethod
    def _create_sqlite_store():
        """Create SQLite event store with default configuration."""
        return SQLiteEventStore()

    @staticmethod
    def _create_postgres_store():
        """Create PostgreSQL event store from environment configuration."""
        config = {
            'host': os.getenv('OBSCURA_PG_HOST', 'localhost'),
            'port': int(os.getenv('OBSCURA_PG_PORT', '5432')),
            'database': os.getenv('OBSCURA_PG_DATABASE', 'obscura'),
            'user': os.getenv('OBSCURA_PG_USER', 'obscura_user'),
            'password': os.getenv('OBSCURA_PG_PASSWORD'),
            'min_connections': int(os.getenv('OBSCURA_PG_MIN_CONNECTIONS', '2')),
            'max_connections': int(os.getenv('OBSCURA_PG_MAX_CONNECTIONS', '10')),
        }

        if not config['password']:
            raise ValueError(
                "OBSCURA_PG_PASSWORD environment variable is required for PostgreSQL. "
                "Set it to your database password."
            )

        return PostgreSQLEventStore(**config)


# Convenience function for backwards compatibility
def get_event_store(db_type: Optional[str] = None):
    """Get an event store instance.

    This is a convenience wrapper around DatabaseFactory.create_event_store().

    Args:
        db_type: Database type ('sqlite' or 'postgresql').
                If None, uses OBSCURA_DB_TYPE environment variable.

    Returns:
        Event store instance
    """
    return DatabaseFactory.create_event_store(db_type)
