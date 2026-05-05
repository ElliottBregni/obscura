"""Shared Postgres testcontainers fixture for the data-layer integration tests.

A single Postgres 16 container is started once per test session and
shared across every test that asks for ``pg_container``. Each test
truncates the relevant tables to keep state isolated — far cheaper
than restarting the container per test.

Tests in this directory are marked ``@pytest.mark.integration`` and
require Docker to be running. They are skipped if testcontainers can't
launch a container (e.g. CI without Docker).
"""

from __future__ import annotations

import os
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest

try:
    from testcontainers.postgres import PostgresContainer

    HAS_TESTCONTAINERS = True
except ImportError:  # pragma: no cover - dev dep, not present in prod
    HAS_TESTCONTAINERS = False
    PostgresContainer = None  # type: ignore[misc, assignment]


def _docker_available() -> bool:
    """Detect whether the test runner can start a docker container."""
    if not HAS_TESTCONTAINERS:
        return False
    import shutil

    return shutil.which("docker") is not None


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="docker / testcontainers not available",
)


@pytest.fixture(scope="session")
def pg_container() -> Generator[Any]:
    """Spin up a Postgres 16 container once per session."""
    if not _docker_available():
        pytest.skip("docker not available")
    container = PostgresContainer("postgres:16-alpine")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture()
def pg_env(pg_container: Any) -> Generator[dict[str, str]]:
    """Populate ``OBSCURA_PG_*`` env vars from the container, reset state.

    Resets the singleton ``PGPoolManager`` and any per-class
    ``_schema_initialized`` flags so each test re-runs the schema DDL
    against a clean state. We don't drop the database — TRUNCATE is
    cheaper and avoids re-running CREATE TABLE for every test.
    """
    env = {
        "OBSCURA_PG_HOST": pg_container.get_container_host_ip(),
        "OBSCURA_PG_PORT": str(pg_container.get_exposed_port(5432)),
        "OBSCURA_PG_USER": pg_container.username,
        "OBSCURA_PG_PASSWORD": pg_container.password,
        "OBSCURA_PG_DATABASE": pg_container.dbname,
        "OBSCURA_DB_URL": "",  # ensure URL doesn't override
    }
    # Reset module-level state so each test reseeds schema.
    from obscura.core.pg_config import PGPoolManager

    PGPoolManager._pool = None  # type: ignore[attr-defined]  # noqa: SLF001
    PGPoolManager._config = None  # type: ignore[attr-defined]  # noqa: SLF001
    from obscura.data.events.postgres import PostgresEventRepo
    from obscura.data.tasks.postgres import PostgresTaskRepo

    PostgresEventRepo._schema_initialized = False  # noqa: SLF001
    PostgresTaskRepo._schema_initialized = False  # noqa: SLF001

    with patch.dict(os.environ, env, clear=False) as _env:
        _env.pop("OBSCURA_DB_URL", None)  # ensure not present
        yield env

    # Truncate tables for the next test — preserves the schema, drops data.
    try:
        from obscura.data.engine import postgres_connection

        with postgres_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DO $$ BEGIN "
                    "IF EXISTS (SELECT FROM information_schema.tables "
                    "WHERE table_name='obscura_events') THEN "
                    "TRUNCATE obscura_events, obscura_sessions; END IF; "
                    "IF EXISTS (SELECT FROM information_schema.tables "
                    "WHERE table_name='obscura_tasks') THEN "
                    "TRUNCATE obscura_tasks; END IF; "
                    "END $$;",
                )
            conn.commit()
        # Close the pool so the next test re-creates it with the right env.
        PGPoolManager.close()
        PGPoolManager._pool = None  # type: ignore[attr-defined]  # noqa: SLF001
        PGPoolManager._config = None  # type: ignore[attr-defined]  # noqa: SLF001
    except Exception:
        # Cleanup best-effort; container teardown will catch the rest.
        pass
