"""Tests for obscura.core.tool_observability.

Covers TurnToolStats record shape, the observer registry (register /
unregister / clear), default-logger emission, and observer-exception
isolation.
"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import logging

import pytest

from obscura.core.tool_observability import (
    TurnToolStats,
    clear_observers,
    emit_turn_tool_stats,
    register_observer,
    unregister_observer,
)


@pytest.fixture(autouse=True)
def clean_observers() -> None:
    """Each test starts with no observers registered."""
    clear_observers()


def _stats(**overrides: object) -> TurnToolStats:
    base = {
        "backend": "openai",
        "registry_total": 100,
        "core_count": 15,
        "discovered_count": 2,
        "sent_count": 17,
        "dropped": ("foo", "bar"),
    }
    base.update(overrides)  # type: ignore[arg-type]
    return TurnToolStats(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TurnToolStats record
# ---------------------------------------------------------------------------


def test_dropped_count_derived_from_dropped() -> None:
    s = _stats(dropped=("a", "b", "c"))
    assert s.dropped_count == 3


def test_short_summary_format() -> None:
    s = _stats()
    summary = s.short()
    assert "[openai]" in summary
    assert "sent=17" in summary
    assert "core=15" in summary
    assert "discovered=2" in summary
    assert "dropped=2" in summary
    assert "registry=100" in summary


# ---------------------------------------------------------------------------
# Observer registry
# ---------------------------------------------------------------------------


def test_register_observer_receives_stats() -> None:
    received: list[TurnToolStats] = []
    register_observer(received.append)

    emit_turn_tool_stats(_stats())
    assert len(received) == 1
    assert received[0].sent_count == 17


def test_register_observer_idempotent() -> None:
    """Same callback registered twice still receives one call per emit."""
    received: list[TurnToolStats] = []
    register_observer(received.append)
    register_observer(received.append)  # ignored

    emit_turn_tool_stats(_stats())
    assert len(received) == 1


def test_unregister_observer_stops_calls() -> None:
    received: list[TurnToolStats] = []
    register_observer(received.append)
    unregister_observer(received.append)

    emit_turn_tool_stats(_stats())
    assert received == []


def test_unregister_unknown_callback_is_noop() -> None:
    def _never_registered(_s: TurnToolStats) -> None:
        return

    # Should not raise.
    unregister_observer(_never_registered)


def test_clear_observers_drops_all() -> None:
    received1: list[TurnToolStats] = []
    received2: list[TurnToolStats] = []
    register_observer(received1.append)
    register_observer(received2.append)
    clear_observers()

    emit_turn_tool_stats(_stats())
    assert received1 == []
    assert received2 == []


# ---------------------------------------------------------------------------
# Default logger + observer-exception isolation
# ---------------------------------------------------------------------------


def test_default_logger_emits_at_info(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="obscura.core.tool_observability"):
        emit_turn_tool_stats(_stats())
    assert any("[openai] tools sent=17" in r.message for r in caplog.records)


def test_observer_exception_does_not_break_emit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A buggy observer must not block the next observer or the caller."""
    after: list[TurnToolStats] = []

    def _broken(_s: TurnToolStats) -> None:
        msg = "boom"
        raise RuntimeError(msg)

    register_observer(_broken)
    register_observer(after.append)

    with caplog.at_level(logging.DEBUG, logger="obscura.core.tool_observability"):
        emit_turn_tool_stats(_stats())  # must not raise

    # The non-broken observer still got the stats.
    assert len(after) == 1
    # And the breakage was logged at debug level.
    assert any("observer" in r.message and "raised" in r.message for r in caplog.records)
