"""Comprehensive tests for LazyPluginManager and plugin adapters."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obscura.plugins.adapters.cli import CLIAdapter, _make_cli_handler
from obscura.plugins.adapters.content import ContentAdapter
from obscura.plugins.adapters.native import NativeAdapter, _resolve_handler
from obscura.plugins.lazy import LazyPluginEntry, LazyPluginManager, LazyState
from obscura.plugins.models import (
    CapabilitySpec,
    HealthcheckSpec,
    PluginSpec,
    ToolContribution,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spec(
    plugin_id: str = "test-plugin",
    tools: tuple[ToolContribution, ...] | None = None,
    runtime_type: str = "native",
    **kwargs: Any,
) -> PluginSpec:
    """Build a minimal valid PluginSpec for testing."""
    if tools is None:
        tools = (
            ToolContribution(name="test_tool", description="A test tool"),
        )
    return PluginSpec(
        id=plugin_id,
        name=kwargs.pop("name", plugin_id),
        version=kwargs.pop("version", "1.0.0"),
        source_type=kwargs.pop("source_type", "local"),
        runtime_type=runtime_type,
        tools=tools,
        **kwargs,
    )


@dataclass
class _MockTool:
    """Tool-like object with a ``handler`` attribute (as adapters expect)."""
    name: str
    handler: str = ""


def _spec_with_handler_tools(
    plugin_id: str = "test-plugin",
    handler_tools: tuple[_MockTool, ...] | None = None,
    runtime_type: str = "native",
    **kwargs: Any,
) -> PluginSpec:
    """Build a PluginSpec whose ``tools`` carry a ``.handler`` attribute.

    The adapters (native, cli) access ``tool.handler`` rather than
    ``tool.handler_ref``, so we substitute mock tool objects to match
    that expectation.
    """
    if handler_tools is None:
        handler_tools = (_MockTool(name="test_tool", handler="json:loads"),)
    # PluginSpec is frozen — we build a real one then swap tools via object.__setattr__
    spec = _make_spec(plugin_id=plugin_id, runtime_type=runtime_type, **kwargs)
    object.__setattr__(spec, "tools", handler_tools)
    return spec


# ═══════════════════════════════════════════════════════════════════════════
# LazyPluginManager
# ═══════════════════════════════════════════════════════════════════════════

class TestLazyPluginManagerRegister:
    """Tests for register() and initial state."""

    def test_register_sets_state_ready(self) -> None:
        mgr = LazyPluginManager(init_fn=MagicMock())
        spec = _make_spec()
        mgr.register(spec)
        assert mgr.get_state("test-plugin") == LazyState.READY

    def test_register_creates_tool_mapping(self) -> None:
        mgr = LazyPluginManager(init_fn=MagicMock())
        spec = _make_spec(tools=(
            ToolContribution(name="alpha", description="a"),
            ToolContribution(name="beta", description="b"),
        ))
        mgr.register(spec)
        # Both tools should resolve to plugin
        assert mgr.ensure_tool_ready("alpha")
        assert mgr.ensure_tool_ready("beta")

    def test_register_appears_in_all_entries(self) -> None:
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_make_spec())
        entries = mgr.all_entries()
        assert "test-plugin" in entries
        assert isinstance(entries["test-plugin"], LazyPluginEntry)


class TestEnsureToolReady:
    """Tests for ensure_tool_ready() lazy init behaviour."""

    def test_unknown_tool_returns_false(self) -> None:
        mgr = LazyPluginManager(init_fn=MagicMock())
        assert mgr.ensure_tool_ready("no_such_tool") is False

    def test_triggers_init_fn_and_state_active(self) -> None:
        init = MagicMock()
        mgr = LazyPluginManager(init_fn=init)
        spec = _make_spec()
        mgr.register(spec)

        result = mgr.ensure_tool_ready("test_tool")

        assert result is True
        init.assert_called_once_with(spec)
        assert mgr.get_state("test-plugin") == LazyState.ACTIVE

    def test_already_active_skips_reinit(self) -> None:
        init = MagicMock()
        mgr = LazyPluginManager(init_fn=init)
        mgr.register(_make_spec())

        mgr.ensure_tool_ready("test_tool")
        mgr.ensure_tool_ready("test_tool")

        init.assert_called_once()  # not called twice

    def test_failed_init_sets_state_failed(self) -> None:
        init = MagicMock(side_effect=RuntimeError("boom"))
        mgr = LazyPluginManager(init_fn=init)
        mgr.register(_make_spec())

        assert mgr.ensure_tool_ready("test_tool") is False
        assert mgr.get_state("test-plugin") == LazyState.FAILED

    def test_subsequent_call_after_failure_returns_false(self) -> None:
        init = MagicMock(side_effect=RuntimeError("boom"))
        mgr = LazyPluginManager(init_fn=init)
        mgr.register(_make_spec())

        mgr.ensure_tool_ready("test_tool")
        assert mgr.ensure_tool_ready("test_tool") is False
        # init_fn should only have been attempted once
        init.assert_called_once()

    def test_use_count_increments(self) -> None:
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_make_spec())

        mgr.ensure_tool_ready("test_tool")
        mgr.ensure_tool_ready("test_tool")
        mgr.ensure_tool_ready("test_tool")

        entry = mgr.all_entries()["test-plugin"]
        assert entry.use_count == 3


class TestSuspendResume:
    """Tests for suspend() and resume()."""

    def test_suspend_active_plugin(self) -> None:
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_make_spec())
        mgr.ensure_tool_ready("test_tool")

        assert mgr.suspend("test-plugin") is True
        assert mgr.get_state("test-plugin") == LazyState.SUSPENDED

    def test_suspend_non_active_returns_false(self) -> None:
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_make_spec())
        assert mgr.suspend("test-plugin") is False  # still READY, not ACTIVE

    def test_resume_reinitializes(self) -> None:
        init = MagicMock()
        mgr = LazyPluginManager(init_fn=init)
        mgr.register(_make_spec())
        mgr.ensure_tool_ready("test_tool")
        mgr.suspend("test-plugin")

        result = mgr.resume("test-plugin")

        assert result is True
        assert mgr.get_state("test-plugin") == LazyState.ACTIVE
        assert init.call_count == 2  # initial + resume

    def test_resume_non_suspended_returns_false(self) -> None:
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_make_spec())
        assert mgr.resume("test-plugin") is False


class TestPrewarm:
    """Prewarm immediately initializes selected plugins."""

    def test_prewarm_triggers_init_at_register(self) -> None:
        init = MagicMock()
        mgr = LazyPluginManager(init_fn=init, prewarm={"test-plugin"})
        spec = _make_spec()
        mgr.register(spec)

        init.assert_called_once_with(spec)
        assert mgr.get_state("test-plugin") == LazyState.ACTIVE

    def test_prewarm_does_not_affect_other_plugins(self) -> None:
        init = MagicMock()
        mgr = LazyPluginManager(init_fn=init, prewarm={"test-plugin"})
        mgr.register(_make_spec())
        mgr.register(_make_spec(plugin_id="other-plugin", tools=(
            ToolContribution(name="other_tool", description="o"),
        )))
        # init called once for prewarmed plugin
        init.assert_called_once()
        assert mgr.get_state("other-plugin") == LazyState.READY


class TestStatsAndQueries:
    """Tests for stats(), active_plugins(), all_entries()."""

    def test_stats_counts_per_state(self) -> None:
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_make_spec(plugin_id="plug-a", tools=(
            ToolContribution(name="tool_a", description="a"),
        )))
        mgr.register(_make_spec(plugin_id="plug-b", tools=(
            ToolContribution(name="tool_b", description="b"),
        )))
        mgr.ensure_tool_ready("tool_a")  # plug-a → ACTIVE

        stats = mgr.stats()
        assert stats.get("active") == 1
        assert stats.get("ready") == 1

    def test_active_plugins(self) -> None:
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_make_spec(plugin_id="plug-a", tools=(
            ToolContribution(name="tool_a", description="a"),
        )))
        mgr.register(_make_spec(plugin_id="plug-b", tools=(
            ToolContribution(name="tool_b", description="b"),
        )))
        mgr.ensure_tool_ready("tool_a")

        assert mgr.active_plugins() == ["plug-a"]


class TestMultiplePluginIsolation:
    """Tools from plugin A must NOT trigger init of plugin B."""

    def test_tool_from_a_does_not_init_b(self) -> None:
        call_log: list[str] = []

        def init_fn(spec: PluginSpec) -> None:
            call_log.append(spec.id)

        mgr = LazyPluginManager(init_fn=init_fn)
        mgr.register(_make_spec(plugin_id="plug-a", tools=(
            ToolContribution(name="tool_a", description="a"),
        )))
        mgr.register(_make_spec(plugin_id="plug-b", tools=(
            ToolContribution(name="tool_b", description="b"),
        )))

        mgr.ensure_tool_ready("tool_a")

        assert call_log == ["plug-a"]
        assert mgr.get_state("plug-b") == LazyState.READY


# ═══════════════════════════════════════════════════════════════════════════
# NativeAdapter
# ═══════════════════════════════════════════════════════════════════════════

class TestNativeAdapterCanHandle:
    def test_native_returns_true(self) -> None:
        adapter = NativeAdapter()
        spec = _make_spec(runtime_type="native")
        assert adapter.can_handle(spec) is True

    def test_cli_returns_false(self) -> None:
        adapter = NativeAdapter()
        spec = _make_spec(runtime_type="cli")
        assert adapter.can_handle(spec) is False


class TestNativeAdapterLoad:
    def test_resolves_handler_refs(self) -> None:
        import json

        adapter = NativeAdapter()
        spec = _spec_with_handler_tools(
            handler_tools=(
                _MockTool(name="json_loads", handler="json:loads"),
                _MockTool(name="json_dumps", handler="json:dumps"),
            ),
        )

        result = asyncio.get_event_loop().run_until_complete(adapter.load(spec, {}))

        assert "handlers" in result
        assert result["handlers"]["json_loads"] is json.loads
        assert result["handlers"]["json_dumps"] is json.dumps

    def test_skips_tools_without_handler(self) -> None:
        adapter = NativeAdapter()
        spec = _spec_with_handler_tools(
            handler_tools=(_MockTool(name="no_handler", handler=""),),
        )

        result = asyncio.get_event_loop().run_until_complete(adapter.load(spec, {}))
        assert result["handlers"] == {}

    def test_unresolvable_handler_logged_but_skipped(self) -> None:
        adapter = NativeAdapter()
        spec = _spec_with_handler_tools(
            handler_tools=(_MockTool(name="bad", handler="nonexistent.mod:func"),),
        )

        result = asyncio.get_event_loop().run_until_complete(adapter.load(spec, {}))
        assert "bad" not in result["handlers"]


class TestNativeAdapterHealthcheck:
    def test_default_healthcheck_returns_true(self) -> None:
        adapter = NativeAdapter()
        spec = _make_spec(runtime_type="native")
        assert asyncio.get_event_loop().run_until_complete(adapter.healthcheck(spec)) is True

    def test_callable_healthcheck_invoked(self) -> None:
        adapter = NativeAdapter()
        spec = _make_spec(
            runtime_type="native",
            healthcheck=HealthcheckSpec(type="callable", target="os.path:exists"),
        )
        # os.path.exists() with no args raises TypeError → returns False
        assert asyncio.get_event_loop().run_until_complete(adapter.healthcheck(spec)) is False


class TestResolveHandler:
    def test_colon_notation(self) -> None:
        import json
        assert _resolve_handler("json:loads") is json.loads

    def test_dot_notation(self) -> None:
        import os.path
        assert _resolve_handler("os.path.exists") is os.path.exists


# ═══════════════════════════════════════════════════════════════════════════
# CLIAdapter
# ═══════════════════════════════════════════════════════════════════════════

class TestCLIAdapterCanHandle:
    def test_cli_returns_true(self) -> None:
        adapter = CLIAdapter()
        assert adapter.can_handle(_make_spec(runtime_type="cli")) is True

    def test_native_returns_false(self) -> None:
        adapter = CLIAdapter()
        assert adapter.can_handle(_make_spec(runtime_type="native")) is False

    def test_content_returns_false(self) -> None:
        adapter = CLIAdapter()
        assert adapter.can_handle(_make_spec(runtime_type="content")) is False


class TestCLIAdapterLoad:
    def test_creates_async_handlers(self) -> None:
        adapter = CLIAdapter()
        spec = _spec_with_handler_tools(
            runtime_type="cli",
            handler_tools=(
                _MockTool(name="my_grep", handler="grep {pattern} {file}"),
            ),
        )

        result = asyncio.get_event_loop().run_until_complete(adapter.load(spec, {}))
        assert "my_grep" in result["handlers"]
        assert asyncio.iscoroutinefunction(result["handlers"]["my_grep"])

    def test_skips_tools_without_handler(self) -> None:
        adapter = CLIAdapter()
        spec = _spec_with_handler_tools(
            runtime_type="cli",
            handler_tools=(_MockTool(name="empty", handler=""),),
        )

        result = asyncio.get_event_loop().run_until_complete(adapter.load(spec, {}))
        assert result["handlers"] == {}


class TestCLIHandlerExecution:
    def test_template_substitution_and_subprocess(self) -> None:
        handler = _make_cli_handler("echo {msg}", "test_echo")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"hello world\n", b"")
        mock_proc.returncode = 0

        with patch("obscura.plugins.adapters.cli.asyncio.create_subprocess_shell",
                    return_value=mock_proc) as mock_shell:
            result = asyncio.get_event_loop().run_until_complete(
                handler(msg="hello world")
            )

        mock_shell.assert_called_once()
        call_cmd = mock_shell.call_args[0][0]
        assert call_cmd == "echo hello world"
        assert result == "hello world\n"

    def test_nonzero_return_raises(self) -> None:
        handler = _make_cli_handler("false", "will_fail")

        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"some error")
        mock_proc.returncode = 1

        with patch("obscura.plugins.adapters.cli.asyncio.create_subprocess_shell",
                    return_value=mock_proc):
            with pytest.raises(RuntimeError, match="failed.*rc=1"):
                asyncio.get_event_loop().run_until_complete(handler())


# ═══════════════════════════════════════════════════════════════════════════
# ContentAdapter
# ═══════════════════════════════════════════════════════════════════════════

class TestContentAdapterCanHandle:
    def test_content_returns_true(self) -> None:
        adapter = ContentAdapter()
        assert adapter.can_handle(_make_spec(runtime_type="content")) is True

    def test_native_returns_false(self) -> None:
        adapter = ContentAdapter()
        assert adapter.can_handle(_make_spec(runtime_type="native")) is False

    def test_cli_returns_false(self) -> None:
        adapter = ContentAdapter()
        assert adapter.can_handle(_make_spec(runtime_type="cli")) is False


class TestContentAdapterLoad:
    def test_returns_empty_handlers(self) -> None:
        adapter = ContentAdapter()
        spec = _make_spec(runtime_type="content")

        result = asyncio.get_event_loop().run_until_complete(adapter.load(spec, {}))
        assert result == {"handlers": {}}


class TestContentAdapterHealthcheck:
    def test_always_true(self) -> None:
        adapter = ContentAdapter()
        spec = _make_spec(runtime_type="content")
        assert asyncio.get_event_loop().run_until_complete(adapter.healthcheck(spec)) is True
