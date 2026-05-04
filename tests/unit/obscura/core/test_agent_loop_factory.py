"""Tests for the v1↔v2 toggle factory.

Exercises:
- ``is_v2_enabled`` env-var detection.
- ``make_agent_loop`` returns ``AgentLoop`` (v1) by default.
- ``make_agent_loop`` returns ``AgentLoopV2`` when ``OBSCURA_AGENT_LOOP=v2``.
- v1 kwargs translate to the right v2 middleware/hook composition.
- Unsupported v1 kwargs are dropped with a one-time WARNING.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from unittest.mock import patch

import pytest

from obscura.core.agent_loop_factory import is_v2_enabled, make_agent_loop
from obscura.core.tools import ToolRegistry


# ---------------------------------------------------------------------------
# is_v2_enabled
# ---------------------------------------------------------------------------


class TestIsV2Enabled:
    def test_default_is_false(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OBSCURA_AGENT_LOOP", None)
            assert is_v2_enabled() is False

    @pytest.mark.parametrize("value", ["v2", "1", "true", "TRUE", "yes", "on"])
    def test_truthy_values(self, value: str) -> None:
        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": value}):
            assert is_v2_enabled() is True

    @pytest.mark.parametrize("value", ["v1", "0", "false", "no", "off", "", "  "])
    def test_falsy_values(self, value: str) -> None:
        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": value}):
            assert is_v2_enabled() is False


# ---------------------------------------------------------------------------
# make_agent_loop selection
# ---------------------------------------------------------------------------


class _StubBackend:
    """Minimal backend stub — no real streaming needed for factory tests."""

    name = "stub"

    @property
    def capabilities(self) -> Any:
        from obscura.core.types import BackendCapabilities

        return BackendCapabilities()

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def stream(self, messages: Any = None, **_kwargs: Any) -> Any:
        # Empty stream — adequate for instantiation tests.
        if False:
            yield None  # pragma: no cover


class TestMakeAgentLoopSelection:
    def test_default_returns_v1(self) -> None:
        from obscura.core.agent_loop import AgentLoop

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OBSCURA_AGENT_LOOP", None)
            loop = make_agent_loop(_StubBackend(), ToolRegistry())  # type: ignore[arg-type]
            assert isinstance(loop, AgentLoop)

    def test_env_v2_returns_v2(self) -> None:
        from obscura.core.agent_loop_v2 import AgentLoopV2

        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": "v2"}):
            loop = make_agent_loop(_StubBackend(), ToolRegistry())  # type: ignore[arg-type]
            assert isinstance(loop, AgentLoopV2)


# ---------------------------------------------------------------------------
# v1 kwarg translation under v2
# ---------------------------------------------------------------------------


class TestV1KwargTranslation:
    def test_capability_token_becomes_capability_gate_middleware(self) -> None:
        class _Token:
            def allows(self, _name: str) -> bool:
                return True

        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": "v2"}):
            loop = make_agent_loop(
                _StubBackend(),
                ToolRegistry(),  # type: ignore[arg-type]
                capability_token=_Token(),
            )
            # Inspect: the v2 instance has at least one dispatch middleware.
            assert len(loop._dispatch_middleware) >= 1  # type: ignore[attr-defined]

    def test_tool_allowlist_becomes_middleware(self) -> None:
        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": "v2"}):
            loop = make_agent_loop(
                _StubBackend(),
                ToolRegistry(),  # type: ignore[arg-type]
                tool_allowlist=["a", "b"],
            )
            assert len(loop._dispatch_middleware) >= 1  # type: ignore[attr-defined]

    def test_hooks_becomes_hook_middleware(self) -> None:
        class _Hooks:
            def run(self, *_args: Any) -> None:
                pass

        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": "v2"}):
            loop = make_agent_loop(
                _StubBackend(),
                ToolRegistry(),  # type: ignore[arg-type]
                hooks=_Hooks(),
            )
            assert len(loop._dispatch_middleware) >= 1  # type: ignore[attr-defined]

    def test_on_confirm_becomes_confirmation_middleware(self) -> None:
        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": "v2"}):
            loop = make_agent_loop(
                _StubBackend(),
                ToolRegistry(),  # type: ignore[arg-type]
                on_confirm=lambda _node: True,
            )
            assert len(loop._dispatch_middleware) >= 1  # type: ignore[attr-defined]

    def test_full_v1_kwargs_compose_correctly(self) -> None:
        class _Token:
            def allows(self, _name: str) -> bool:
                return True

        class _Hooks:
            def run(self, *_args: Any) -> None:
                pass

        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": "v2"}):
            loop = make_agent_loop(
                _StubBackend(),
                ToolRegistry(),  # type: ignore[arg-type]
                capability_token=_Token(),
                tool_allowlist=["a"],
                on_confirm=lambda _n: True,
                hooks=_Hooks(),
                tool_output_overrides={"a": "silent"},
            )
            # Five v1 features → five middleware entries.
            assert len(loop._dispatch_middleware) == 5  # type: ignore[attr-defined]

    def test_compaction_pre_turn_when_context_budget_set(self) -> None:
        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": "v2"}):
            loop = make_agent_loop(
                _StubBackend(),
                ToolRegistry(),  # type: ignore[arg-type]
                context_budget=100_000,
                model_name="claude-sonnet-4-5",
            )
            assert loop._pre_turn is not None  # type: ignore[attr-defined]

    def test_event_store_post_turn_hook(self) -> None:
        class _Store:
            def append(self, *_args: Any) -> None:
                pass

        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": "v2"}):
            loop = make_agent_loop(
                _StubBackend(),
                ToolRegistry(),  # type: ignore[arg-type]
                event_store=_Store(),
            )
            assert loop._post_turn is not None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Unsupported kwargs warn
# ---------------------------------------------------------------------------


class TestUnsupportedKwargsWarn:
    def test_unsupported_kwarg_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Reset the dedup so the warning fires deterministically.
        from obscura.core.agent_loop_factory import _warned_unsupported

        _warned_unsupported.discard("compiled_agent")

        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": "v2"}):
            with caplog.at_level(logging.WARNING):
                make_agent_loop(
                    _StubBackend(),
                    ToolRegistry(),  # type: ignore[arg-type]
                    compiled_agent="something",
                )
            assert any("compiled_agent" in r.message for r in caplog.records)
