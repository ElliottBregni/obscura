"""Unit tests for obscura.tools.providers.agent_primitives.

Coverage targets:
  - _http_json: success, JSON vs. plain-text body, HTTP error, network error
  - _env: present vs. missing env var
  - _run_cli: success, nonzero exit, timeout, exception
  - fzf_filter: success, exception path
  - duckdb_query: success, bad SQL
  - healthcheck: trivial
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

import obscura.tools.providers.agent_primitives as _ap

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _env
# ---------------------------------------------------------------------------


def test_env_present_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_SECRET", "abc123")
    assert _ap._env("MY_SECRET") == "abc123"


def test_env_missing_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MY_SECRET", raising=False)
    with pytest.raises(ValueError, match="MY_SECRET"):
        _ap._env("MY_SECRET")


def test_env_empty_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_SECRET", "   ")
    with pytest.raises(ValueError, match="MY_SECRET"):
        _ap._env("MY_SECRET")


# ---------------------------------------------------------------------------
# _http_json — basic paths
# ---------------------------------------------------------------------------


@respx.mock
async def test_http_json_success_json_body() -> None:
    respx.get("https://example.com/data").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )

    result = await _ap._http_json(method="GET", url="https://example.com/data")

    assert result["ok"] is True
    assert result["status_code"] == 200
    assert result["data"] == {"result": "ok"}


@respx.mock
async def test_http_json_plain_text_wrapped() -> None:
    respx.get("https://example.com/txt").mock(
        return_value=httpx.Response(
            200, text="hello world", headers={"content-type": "text/plain"}
        )
    )

    result = await _ap._http_json(method="GET", url="https://example.com/txt")

    assert result["ok"] is True
    assert "text" in result["data"]
    assert "hello world" in result["data"]["text"]


@respx.mock
async def test_http_json_http_error_sets_ok_false() -> None:
    respx.get("https://example.com/fail").mock(
        return_value=httpx.Response(500, json={"error": "server error"})
    )

    result = await _ap._http_json(method="GET", url="https://example.com/fail")

    assert result["ok"] is False
    assert result["status_code"] == 500
    assert "error" in result


@respx.mock
async def test_http_json_post_with_json_body() -> None:
    route = respx.post("https://example.com/create").mock(
        return_value=httpx.Response(201, json={"id": "new-1"})
    )

    result = await _ap._http_json(
        method="POST",
        url="https://example.com/create",
        json_body={"name": "test"},
    )

    assert result["ok"] is True
    assert route.called


async def test_http_json_network_error_returns_error() -> None:
    with respx.mock:
        respx.get("https://unreachable.example.com/").mock(
            side_effect=httpx.ConnectError("no route")
        )

        result = await _ap._http_json(
            method="GET", url="https://unreachable.example.com/"
        )

    assert result["ok"] is False
    assert "error" in result
    assert "duration_seconds" in result


@respx.mock
async def test_http_json_includes_duration() -> None:
    respx.get("https://example.com/").mock(return_value=httpx.Response(200, json={}))

    result = await _ap._http_json(method="GET", url="https://example.com/")

    assert "duration_seconds" in result
    assert isinstance(result["duration_seconds"], float)


# ---------------------------------------------------------------------------
# _run_cli
# ---------------------------------------------------------------------------


def test_run_cli_success() -> None:
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "hello\nworld\n"
    proc.stderr = ""

    with patch.object(_ap.subprocess, "run", return_value=proc):
        result = _ap._run_cli(["echo", "hello", "world"])

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]


def test_run_cli_nonzero_exit() -> None:
    proc = MagicMock()
    proc.returncode = 1
    proc.stdout = ""
    proc.stderr = "command not found"

    with patch.object(_ap.subprocess, "run", return_value=proc):
        result = _ap._run_cli(["bad-command"])

    assert result["ok"] is False
    assert result["exit_code"] == 1


def test_run_cli_timeout_returns_error() -> None:
    with patch.object(
        _ap.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(["cmd"], 60),
    ):
        result = _ap._run_cli(["slow-command"])

    assert result["ok"] is False
    assert result["error"] == "timeout"


def test_run_cli_file_not_found_returns_error() -> None:
    with patch.object(
        _ap.subprocess,
        "run",
        side_effect=FileNotFoundError("no such binary"),
    ):
        result = _ap._run_cli(["nonexistent-binary"])

    assert result["ok"] is False
    assert "FileNotFoundError" in result["error"]


def test_run_cli_large_stdout_truncated() -> None:
    big_output = "x\n" * 60_000  # > _MAX_CLI_OUTPUT (100_000 chars)
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = big_output
    proc.stderr = ""

    with patch.object(_ap.subprocess, "run", return_value=proc):
        result = _ap._run_cli(["big-output-cmd"])

    assert result.get("stdout_truncated") is True
    assert len(result["stdout"]) <= _ap._MAX_CLI_OUTPUT


# ---------------------------------------------------------------------------
# fzf_filter
# ---------------------------------------------------------------------------


def test_fzf_filter_success() -> None:
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "alpha\nbeta\n"
    proc.stderr = ""

    with patch.object(_ap.subprocess, "run", return_value=proc):
        result = _ap.fzf_filter("al", "alpha\nbeta\ngamma\n")

    assert result["ok"] is True
    assert "alpha" in result["matches"]
    assert result["match_count"] == 2


def test_fzf_filter_exception_returns_error() -> None:
    with patch.object(
        _ap.subprocess,
        "run",
        side_effect=FileNotFoundError("fzf not found"),
    ):
        result = _ap.fzf_filter("test", "input")

    assert result["ok"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# duckdb_query
# ---------------------------------------------------------------------------


def test_duckdb_query_select_one() -> None:
    pytest.importorskip("duckdb")
    result = _ap.duckdb_query("SELECT 1 AS n")

    assert result["ok"] is True
    assert result["columns"] == ["n"]
    assert result["rows"] == [(1,)]


def test_duckdb_query_bad_sql_returns_error() -> None:
    result = _ap.duckdb_query("NOT VALID SQL !!!")

    assert result["ok"] is False
    assert "error" in result


# ---------------------------------------------------------------------------
# healthcheck
# ---------------------------------------------------------------------------


async def test_healthcheck_returns_healthy() -> None:
    result = await _ap.healthcheck()

    assert result["ok"] is True
    assert result["status"] == "healthy"
