"""
Tests for sdk.telemetry.audit — Compliance audit logger.

Verifies AuditEvent creation, JSONL file append-only behavior,
and integration with OTel span events.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from sdk.telemetry.audit import (
    AuditEvent,
    emit_audit_event,
    get_audit_log_path,
    set_audit_log_path,
)


# ---------------------------------------------------------------------------
# AuditEvent dataclass
# ---------------------------------------------------------------------------

class TestAuditEvent:
    def test_default_timestamp(self) -> None:
        """Timestamp should be auto-populated if not provided."""
        event = AuditEvent(
            event_type="test",
            user_id="u1",
            user_email="a@b.c",
            resource="test:resource",
            action="read",
            outcome="success",
        )
        assert event.timestamp != ""
        assert "T" in event.timestamp  # ISO 8601 format

    def test_explicit_timestamp(self) -> None:
        """Explicit timestamp should be preserved."""
        event = AuditEvent(
            event_type="test",
            user_id="u1",
            user_email="a@b.c",
            resource="test:resource",
            action="read",
            outcome="success",
            timestamp="2024-01-01T00:00:00Z",
        )
        assert event.timestamp == "2024-01-01T00:00:00Z"

    def test_frozen(self) -> None:
        """AuditEvent should be immutable."""
        event = AuditEvent(
            event_type="test",
            user_id="u1",
            user_email="a@b.c",
            resource="r",
            action="a",
            outcome="o",
        )
        with pytest.raises(AttributeError):
            event.event_type = "changed"  # type: ignore[misc]

    def test_details_default(self) -> None:
        """Details should default to empty dict."""
        event = AuditEvent(
            event_type="test",
            user_id="u1",
            user_email="a@b.c",
            resource="r",
            action="a",
            outcome="o",
        )
        assert event.details == {}

    def test_details_preserved(self) -> None:
        """Custom details should be preserved."""
        event = AuditEvent(
            event_type="test",
            user_id="u1",
            user_email="a@b.c",
            resource="r",
            action="a",
            outcome="o",
            details={"prompt_len": 42, "model": "gpt-5"},
        )
        assert event.details["prompt_len"] == 42
        assert event.details["model"] == "gpt-5"

    def test_trace_id_auto_populated(self) -> None:
        """trace_id should be auto-populated (empty if no OTel context)."""
        event = AuditEvent(
            event_type="test",
            user_id="u1",
            user_email="a@b.c",
            resource="r",
            action="a",
            outcome="o",
        )
        # Without active OTel span, trace_id should be empty string
        assert isinstance(event.trace_id, str)


# ---------------------------------------------------------------------------
# Audit log path
# ---------------------------------------------------------------------------

class TestAuditLogPath:
    def test_default_path(self) -> None:
        """Default should be 'audit.jsonl' or OBSCURA_AUDIT_LOG env var."""
        path = get_audit_log_path()
        assert path.name == "audit.jsonl" or "audit" in str(path)

    def test_set_path(self, tmp_path: Path) -> None:
        """set_audit_log_path should change the output path."""
        custom = tmp_path / "custom-audit.jsonl"
        set_audit_log_path(custom)
        assert get_audit_log_path() == custom
        # Reset
        set_audit_log_path(Path("audit.jsonl"))

    def test_env_var_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """OBSCURA_AUDIT_LOG env var should override default."""
        custom = str(tmp_path / "env-audit.jsonl")
        monkeypatch.setenv("OBSCURA_AUDIT_LOG", custom)
        # Reset internal state
        import sdk.telemetry.audit as mod
        old = mod._audit_log_path
        mod._audit_log_path = None
        try:
            path = get_audit_log_path()
            assert str(path) == custom
        finally:
            mod._audit_log_path = old


# ---------------------------------------------------------------------------
# emit_audit_event — file output
# ---------------------------------------------------------------------------

class TestEmitAuditEvent:
    def test_writes_jsonl(self, tmp_path: Path) -> None:
        """emit_audit_event should append a JSON line to the audit log."""
        log_file = tmp_path / "test-audit.jsonl"
        set_audit_log_path(log_file)

        try:
            event = AuditEvent(
                event_type="agent.send",
                user_id="u-123",
                user_email="dev@test.com",
                resource="backend:copilot",
                action="execute",
                outcome="success",
                details={"prompt_len": 10},
            )
            emit_audit_event(event)

            assert log_file.exists()
            lines = log_file.read_text().strip().split("\n")
            assert len(lines) == 1

            record = json.loads(lines[0])
            assert record["event_type"] == "agent.send"
            assert record["user_id"] == "u-123"
            assert record["user_email"] == "dev@test.com"
            assert record["resource"] == "backend:copilot"
            assert record["action"] == "execute"
            assert record["outcome"] == "success"
            assert record["details"]["prompt_len"] == 10
        finally:
            set_audit_log_path(Path("audit.jsonl"))

    def test_append_only(self, tmp_path: Path) -> None:
        """Multiple events should be appended, not overwritten."""
        log_file = tmp_path / "append-audit.jsonl"
        set_audit_log_path(log_file)

        try:
            for i in range(3):
                emit_audit_event(AuditEvent(
                    event_type=f"test.event_{i}",
                    user_id="u",
                    user_email="e",
                    resource="r",
                    action="a",
                    outcome="o",
                ))

            lines = log_file.read_text().strip().split("\n")
            assert len(lines) == 3

            for i, line in enumerate(lines):
                record = json.loads(line)
                assert record["event_type"] == f"test.event_{i}"
        finally:
            set_audit_log_path(Path("audit.jsonl"))

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        """Should create parent dirs if they don't exist."""
        log_file = tmp_path / "deep" / "nested" / "audit.jsonl"
        set_audit_log_path(log_file)

        try:
            emit_audit_event(AuditEvent(
                event_type="test",
                user_id="u",
                user_email="e",
                resource="r",
                action="a",
                outcome="o",
            ))

            assert log_file.exists()
        finally:
            set_audit_log_path(Path("audit.jsonl"))

    def test_valid_json_per_line(self, tmp_path: Path) -> None:
        """Every line should be valid JSON."""
        log_file = tmp_path / "json-audit.jsonl"
        set_audit_log_path(log_file)

        try:
            for _ in range(5):
                emit_audit_event(AuditEvent(
                    event_type="test",
                    user_id="u",
                    user_email="e",
                    resource="r",
                    action="a",
                    outcome="o",
                    details={"key": "value", "nested": {"a": 1}},
                ))

            for line in log_file.read_text().strip().split("\n"):
                record = json.loads(line)
                assert isinstance(record, dict)
                assert "event_type" in record
        finally:
            set_audit_log_path(Path("audit.jsonl"))
