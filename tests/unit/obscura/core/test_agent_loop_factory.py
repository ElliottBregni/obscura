"""Tests for the agent-loop factory.

v1 has been removed; ``make_agent_loop`` now always returns an
``AgentLoopV2``. The factory still accepts v1-shape kwargs and translates
them into v2 middleware/hook composition.

Exercises:
- ``is_v2_enabled`` returns True regardless of env (kept for back-compat).
- ``make_agent_loop`` always returns ``AgentLoopV2``.
- ``OBSCURA_AGENT_LOOP=v1`` logs a one-time warning but still uses v2.
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
    """v1 has been removed — is_v2_enabled() now always returns True.

    Kept as a compatibility shim so existing callers that gated on the
    env var continue to compile. The function logs a one-time warning
    when v1 is explicitly requested.
    """

    def test_returns_true_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OBSCURA_AGENT_LOOP", None)
            assert is_v2_enabled() is True

    @pytest.mark.parametrize(
        "value", ["v2", "1", "true", "yes", "on", "anything-else"]
    )
    def test_returns_true_for_v2_synonyms(self, value: str) -> None:
        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": value}):
            assert is_v2_enabled() is True

    @pytest.mark.parametrize("value", ["v1", "0", "false", "no", "off"])
    def test_v1_optout_still_returns_true_with_warning(
        self, value: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Reset the dedup so the warning fires per call in the test.
        from obscura.core.agent_loop_factory import _warned_v1_optout  # noqa: F401

        import obscura.core.agent_loop_factory as factory_mod

        factory_mod._warned_v1_optout = False
        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": value}):
            with caplog.at_level("WARNING"):
                assert is_v2_enabled() is True
            assert any("v1 has been removed" in r.message for r in caplog.records)


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
    def test_default_returns_v2(self) -> None:
        """v2 is the default — unset env uses v2."""
        from obscura.core.agent_loop_v2 import AgentLoopV2

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OBSCURA_AGENT_LOOP", None)
            loop = make_agent_loop(_StubBackend(), ToolRegistry())  # type: ignore[arg-type]
            assert isinstance(loop, AgentLoopV2)

    def test_env_v1_still_returns_v2(self) -> None:
        """v1 has been removed — explicit opt-out logs a warning but
        still returns AgentLoopV2."""
        from obscura.core.agent_loop_v2 import AgentLoopV2

        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": "v1"}):
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
            # Five v1 features + predictive_cache (default-on) → six middleware entries.
            assert len(loop._dispatch_middleware) == 6  # type: ignore[attr-defined]

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
# CompiledAgent translation
# ---------------------------------------------------------------------------


class _FakeCompiledAgent:
    """A minimal stand-in for obscura.core.compiler.compiled.CompiledAgent.

    Tests don't need the real type — duck-typing on the field names is what
    the factory actually relies on.
    """

    def __init__(
        self,
        *,
        instructions: str = "",
        max_iterations: int = 10,
        tool_allowlist: frozenset[str] | None = None,
        tool_denylist: frozenset[str] = frozenset(),
    ) -> None:
        self.instructions = instructions
        self.max_iterations = max_iterations
        self.tool_allowlist = tool_allowlist
        self.tool_denylist = tool_denylist


class TestCompiledAgentTranslation:
    def test_compiled_allowlist_becomes_middleware(self) -> None:
        ca = _FakeCompiledAgent(tool_allowlist=frozenset({"a", "b"}))
        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": "v2"}):
            loop = make_agent_loop(
                _StubBackend(),
                ToolRegistry(),  # type: ignore[arg-type]
                compiled_agent=ca,
            )
            assert len(loop._dispatch_middleware) >= 1  # type: ignore[attr-defined]

    def test_compiled_denylist_adds_middleware(self) -> None:
        ca = _FakeCompiledAgent(tool_denylist=frozenset({"forbidden"}))
        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": "v2"}):
            loop = make_agent_loop(
                _StubBackend(),
                ToolRegistry(),  # type: ignore[arg-type]
                compiled_agent=ca,
            )
            # At least the predictive_cache middleware (default ON) +
            # tool_denylist from compiled.
            assert len(loop._dispatch_middleware) >= 2  # type: ignore[attr-defined]

    def test_compiled_max_iterations_sets_max_turns(self) -> None:
        ca = _FakeCompiledAgent(max_iterations=42)
        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": "v2"}):
            loop = make_agent_loop(
                _StubBackend(),
                ToolRegistry(),  # type: ignore[arg-type]
                compiled_agent=ca,
            )
            assert loop._config.max_turns == 42  # type: ignore[attr-defined]

    def test_explicit_max_turns_overrides_compiled(self) -> None:
        ca = _FakeCompiledAgent(max_iterations=42)
        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": "v2"}):
            loop = make_agent_loop(
                _StubBackend(),
                ToolRegistry(),  # type: ignore[arg-type]
                compiled_agent=ca,
                max_turns=7,
            )
            assert loop._config.max_turns == 7  # type: ignore[attr-defined]

    def test_compiled_instructions_becomes_system_prompt(self) -> None:
        ca = _FakeCompiledAgent(instructions="You are a helpful test agent.")
        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": "v2"}):
            loop = make_agent_loop(
                _StubBackend(),
                ToolRegistry(),  # type: ignore[arg-type]
                compiled_agent=ca,
            )
            assert loop._system_prompt == "You are a helpful test agent."  # type: ignore[attr-defined]

    def test_disable_via_env_flag(self) -> None:
        ca = _FakeCompiledAgent(
            instructions="ignored",
            max_iterations=42,
            tool_allowlist=frozenset({"a"}),
        )
        with patch.dict(
            os.environ,
            {"OBSCURA_AGENT_LOOP": "v2", "OBSCURA_V2_COMPILED_AGENT": "0"},
        ):
            loop = make_agent_loop(
                _StubBackend(),
                ToolRegistry(),  # type: ignore[arg-type]
                compiled_agent=ca,
            )
            # Compiled values ignored — defaults take over.
            assert loop._system_prompt == ""  # type: ignore[attr-defined]
            assert loop._config.max_turns == 10  # default  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Unsupported kwargs warn
# ---------------------------------------------------------------------------


class TestUnsupportedKwargsWarn:
    def test_unsupported_kwarg_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Reset the dedup so the warning fires deterministically.
        from obscura.core.agent_loop_factory import _warned_unsupported

        _warned_unsupported.discard("truly_unknown_kwarg")

        with patch.dict(os.environ, {"OBSCURA_AGENT_LOOP": "v2"}):
            with caplog.at_level(logging.WARNING):
                make_agent_loop(
                    _StubBackend(),
                    ToolRegistry(),  # type: ignore[arg-type]
                    truly_unknown_kwarg=60.0,
                )
            assert any("truly_unknown_kwarg" in r.message for r in caplog.records)
