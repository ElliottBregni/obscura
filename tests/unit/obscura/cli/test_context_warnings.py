from __future__ import annotations

from unittest.mock import Mock

from obscura.cli.__init__ import _copilot_budget_pct, _emit_context_warnings
from obscura.cli.commands import REPLContext


def _ctx(backend: str = "codex") -> REPLContext:
    return REPLContext(
        client=Mock(),
        store=Mock(),
        session_id="s1",
        backend=backend,
        model="gpt-5",
        system_prompt="",
        max_turns=8,
        tools_enabled=True,
    )


def test_emit_context_warnings_on_threshold_cross(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("obscura.cli.__init__.console.print", lambda msg: calls.append(msg))
    ctx = _ctx("codex")

    # 20% -> no warning
    _emit_context_warnings(ctx, tokens=20_000, context_window=100_000)
    assert calls == []

    # 30% -> cross 25
    _emit_context_warnings(ctx, tokens=30_000, context_window=100_000)
    assert len(calls) == 1
    assert "crossed 25%" in calls[-1]

    # 55% -> cross 50 only
    _emit_context_warnings(ctx, tokens=55_000, context_window=100_000)
    assert len(calls) == 2
    assert "crossed 50%" in calls[-1]

    # 80% -> cross 75 only
    _emit_context_warnings(ctx, tokens=80_000, context_window=100_000)
    assert len(calls) == 3
    assert "crossed 75%" in calls[-1]


def test_emit_context_warnings_copilot_includes_budget(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("obscura.cli.__init__.console.print", lambda msg: calls.append(msg))
    ctx = _ctx("copilot")

    _emit_context_warnings(ctx, tokens=30_000, context_window=100_000)
    assert len(calls) == 1
    assert "Copilot budget: 60% of 50,000 token soft budget." in calls[0]


def test_emit_context_warnings_retrigger_after_drop(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr("obscura.cli.__init__.console.print", lambda msg: calls.append(msg))
    ctx = _ctx("codex")

    _emit_context_warnings(ctx, tokens=30_000, context_window=100_000)  # cross 25
    _emit_context_warnings(ctx, tokens=10_000, context_window=100_000)  # drop below 25
    _emit_context_warnings(ctx, tokens=26_000, context_window=100_000)  # cross 25 again
    assert len(calls) == 2
    assert "crossed 25%" in calls[0]
    assert "crossed 25%" in calls[1]


def test_copilot_budget_pct() -> None:
    assert _copilot_budget_pct(tokens=25_000, context_window=100_000) == 50
