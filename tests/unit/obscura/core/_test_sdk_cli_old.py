"""Tests for CLI utility helpers re-exported via obscura.cli.

These tests cover _StderrLogger, _summarize_tool_input, and telemetry
helpers that were originally in the argparse-based CLI and are now in
obscura.cli.chat_cli (re-exported through obscura.cli).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from obscura.cli import _StderrLogger as _StderrLogger
from obscura.cli import _summarize_tool_input as _summarize_tool_input
from obscura.cli import _init_cli_telemetry as _init_cli_telemetry
from obscura.cli import _get_cli_logger as _get_cli_logger


# ---------------------------------------------------------------------------
# StderrLogger
# ---------------------------------------------------------------------------


class TestStderrLogger:
    def test_info(self, capsys: pytest.CaptureFixture[str]) -> None:
        log = _StderrLogger()
        log.info("test.event", msg="hello")
        assert "hello" in capsys.readouterr().err

    def test_info_fallback(self, capsys: pytest.CaptureFixture[str]) -> None:
        log = _StderrLogger()
        log.info("test.event")
        assert "test.event" in capsys.readouterr().err

    def test_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        log = _StderrLogger()
        log.error("test.event", error="bad thing")
        assert "bad thing" in capsys.readouterr().err

    def test_error_fallback_msg(self, capsys: pytest.CaptureFixture[str]) -> None:
        log = _StderrLogger()
        log.error("test.event", msg="error msg")
        assert "error msg" in capsys.readouterr().err

    def test_error_fallback_event(self, capsys: pytest.CaptureFixture[str]) -> None:
        log = _StderrLogger()
        log.error("test.event")
        assert "test.event" in capsys.readouterr().err

    def test_warning(self, capsys: pytest.CaptureFixture[str]) -> None:
        log = _StderrLogger()
        log.warning("test.event", msg="careful")
        assert "careful" in capsys.readouterr().err

    def test_warning_fallback(self, capsys: pytest.CaptureFixture[str]) -> None:
        log = _StderrLogger()
        log.warning("test.event")
        assert "test.event" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _summarize_tool_input
# ---------------------------------------------------------------------------


class TestToolInputSummary:
    def test_summarize_object_keys(self) -> None:
        out = _summarize_tool_input('{"script":"ls","cwd":"/tmp"}')
        assert out == "args keys: script, cwd"

    def test_summarize_non_json(self) -> None:
        out = _summarize_tool_input("raw input value")
        assert out == "args: raw input value"


# ---------------------------------------------------------------------------
# Telemetry helpers
# ---------------------------------------------------------------------------


class TestTelemetryHelpers:
    def test_init_cli_telemetry_no_crash(self) -> None:
        """_init_cli_telemetry should not raise."""
        _init_cli_telemetry()

    def test_get_cli_logger_returns_logger(self) -> None:
        """_get_cli_logger returns something with info/error."""
        log = _get_cli_logger("test")
        assert hasattr(log, "info")
        assert hasattr(log, "error")

    def test_get_cli_logger_fallback(self) -> None:
        """When obscura.telemetry.logging is unavailable, falls back to _StderrLogger."""
        original_import: Any = (
            __builtins__.__import__
            if hasattr(__builtins__, "__import__")
            else __import__
        )

        def mock_import(name: str, *a: Any, **kw: Any) -> Any:
            if name == "obscura.telemetry.logging":
                raise ImportError("missing")
            return original_import(name, *a, **kw)

        with patch("builtins.__import__", side_effect=mock_import):
            log = _get_cli_logger("test")
            assert isinstance(log, _StderrLogger)
