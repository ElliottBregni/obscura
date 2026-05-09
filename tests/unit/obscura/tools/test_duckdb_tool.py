"""Unit tests for duckdb_query primitive.

duckdb_query is a synchronous function that executes SQL against a DuckDB
connection (in-memory by default) and returns a plain dict (not JSON string).
No mocking is needed — each call gets a fresh in-memory database.
"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import pytest

pytest.importorskip("duckdb")

# Import system tools first to prime sys.modules and break the circular
# dependency that agent_primitives → providers.__init__ → tools.system creates.
from obscura.tools.system import get_system_tool_specs as _  # noqa: E402, F401
from obscura.tools.providers.agent_primitives import duckdb_query  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Basic query execution
# ---------------------------------------------------------------------------


def test_select_literal_returns_correct_row() -> None:
    result = duckdb_query("SELECT 42 AS answer")

    assert result["ok"] is True
    assert result["columns"] == ["answer"]
    assert result["row_count"] == 1
    assert result["rows"][0][0] == 42


def test_select_multiple_columns() -> None:
    result = duckdb_query("SELECT 1 AS n, 'hello' AS s, TRUE AS flag")

    assert result["ok"] is True
    assert result["column_count"] == 3
    assert result["columns"] == ["n", "s", "flag"]
    row = result["rows"][0]
    assert row[0] == 1
    assert row[1] == "hello"


def test_select_from_range_function() -> None:
    """DuckDB range() generates a table of integers."""
    result = duckdb_query("SELECT * FROM range(5) AS r(n)")

    assert result["ok"] is True
    assert result["row_count"] == 5
    values = [row[0] for row in result["rows"]]
    assert values == [0, 1, 2, 3, 4]


def test_create_and_query_in_memory_table(tmp_path: object) -> None:
    """Using a file-based database lets CREATE TABLE + SELECT share state."""
    import os

    db_path = str(tmp_path) + "/test.duckdb"  # type: ignore[operator]
    duckdb_query(
        "CREATE TABLE numbers AS SELECT * FROM range(3) AS r(n)", database=db_path
    )
    result = duckdb_query("SELECT n FROM numbers ORDER BY n", database=db_path)

    assert result["ok"] is True
    assert result["row_count"] == 3
    values = [row[0] for row in result["rows"]]
    assert values == [0, 1, 2]
    # Clean up
    os.unlink(db_path)


# ---------------------------------------------------------------------------
# Metadata fields
# ---------------------------------------------------------------------------


def test_result_includes_duration_seconds() -> None:
    result = duckdb_query("SELECT 1")

    assert "duration_seconds" in result
    assert isinstance(result["duration_seconds"], float)
    assert result["duration_seconds"] >= 0.0


def test_empty_result_set() -> None:
    result = duckdb_query("SELECT 1 WHERE FALSE")

    assert result["ok"] is True
    assert result["row_count"] == 0
    assert result["rows"] == []


def test_query_and_database_echoed_back() -> None:
    result = duckdb_query("SELECT 99", database=":memory:")

    assert result["query"] == "SELECT 99"
    assert result["database"] == ":memory:"


# ---------------------------------------------------------------------------
# Truncation at 1000 rows
# ---------------------------------------------------------------------------


def test_row_truncation_at_1000_rows() -> None:
    """Queries returning > 1000 rows are capped and flagged."""
    result = duckdb_query("SELECT * FROM range(1001) AS r(n)")

    assert result["ok"] is True
    assert result["row_count"] == 1001  # true count
    assert len(result["rows"]) == 1000  # capped
    assert result["truncated"] is True


def test_no_truncation_at_1000_rows_exactly() -> None:
    result = duckdb_query("SELECT * FROM range(1000) AS r(n)")

    assert result["ok"] is True
    assert result["row_count"] == 1000
    assert len(result["rows"]) == 1000
    assert result["truncated"] is False


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_syntax_error_returns_error_dict() -> None:
    result = duckdb_query("THIS IS NOT SQL AT ALL !!!!")

    assert result["ok"] is False
    assert "error" in result
    assert "detail" in result


def test_missing_table_returns_error() -> None:
    result = duckdb_query("SELECT * FROM no_such_table_xyz_obscura")

    assert result["ok"] is False
    assert result["ok"] is False


def test_error_result_includes_query_echo() -> None:
    q = "SELECT * FROM nope_xyz"
    result = duckdb_query(q)

    assert result["ok"] is False
    assert result["query"] == q


# ---------------------------------------------------------------------------
# Memory isolation between calls
# ---------------------------------------------------------------------------


def test_in_memory_databases_are_isolated() -> None:
    """Two :memory: connections don't share tables."""
    # Create table in first connection
    r1 = duckdb_query("CREATE TABLE isolated_test_xyz (x INT)")
    assert r1["ok"] is True

    # Second call gets a fresh connection — table is gone
    r2 = duckdb_query("SELECT * FROM isolated_test_xyz")
    assert r2["ok"] is False  # table doesn't exist in new :memory: connection
