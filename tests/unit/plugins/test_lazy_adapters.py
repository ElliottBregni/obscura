"""Comprehensive tests for lazy plugin loading and plugin adapters."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obscura.plugins.adapters.base import PluginAdapter
from obscura.plugins.adapters.cli import CLIAdapter, _make_cli_handler
from obscura.plugins.adapters.content import ContentAdapter
from obscura.plugins.adapters.native import NativeAdapter, _resolve_handler
from obscura.plugins.lazy import LazyPluginEntry, LazyPluginManager, LazyState
from obscura.plugins.models import HealthcheckSpec, PluginSpec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PluginToolSpec:
    """Lightweight tool spec with a ``handler`` attribute (as adapters expect)."""
    name: str
    handler: str = ""
    description: str = ""
    parameters: dict = field(default_factory=dict)
    capability: str = ""
    side_effects: str = "none"
    timeout: float = 30.0
    retry: int = 0


def _spec(
    pid: str = "test",
    runtime: str = "native",
    tools: tuple = (),
    healthcheck: HealthcheckSpec | None = None,
) -> PluginSpec:
    """Build a minimal PluginSpec."""
    return PluginSpec(
        id=pid,
        name=pid.title(),
        version="1.0.0",
        source_type="builtin",
        runtime_type=runtime,
        trust_level="builtin",
        author="test",
        description="test plugin",
        tools=tools,
        healthcheck=healthcheck,
    )


def _tool(name: str = "my-tool", handler: str = "mod:func") -> PluginToolSpec:
    return PluginToolSpec(name=name, handler=handler)


def _tool_no_handler(name: str = "empty-tool") -> PluginToolSpec:
    return PluginToolSpec(name=name, handler="")


# ---------------------------------------------------------------------------
# LazyPluginEntry
# ---------------------------------------------------------------------------


class TestLazyPluginEntry:
    def test_tool_names_returns_set_of_names(self):
        spec = _spec(tools=(_tool("a"), _tool("b")))
        entry = LazyPluginEntry(spec=spec)
        assert entry.tool_names == {"a", "b"}

    def test_tool_names_empty_when_no_tools(self):
        entry = LazyPluginEntry(spec=_spec())
        assert entry.tool_names == set()

    def test_default_state_is_discovered(self):
        entry = LazyPluginEntry(spec=_spec())
        assert entry.state == LazyState.DISCOVERED


# ---------------------------------------------------------------------------
# LazyPluginManager
# ---------------------------------------------------------------------------


class TestLazyPluginManagerRegister:
    def test_register_sets_ready_state(self):
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_spec("alpha"))
        assert mgr.get_state("alpha") == LazyState.READY

    def test_register_maps_tools_to_plugin(self):
        mgr = LazyPluginManager(init_fn=MagicMock())
        spec = _spec("alpha", tools=(_tool("t1"), _tool("t2")))
        mgr.register(spec)
        assert mgr._tool_to_plugin["t1"] == "alpha"
        assert mgr._tool_to_plugin["t2"] == "alpha"

    def test_prewarm_triggers_immediate_init(self):
        init_fn = MagicMock()
        mgr = LazyPluginManager(init_fn=init_fn, prewarm={"alpha"})
        mgr.register(_spec("alpha"))
        init_fn.assert_called_once()
        assert mgr.get_state("alpha") == LazyState.ACTIVE

    def test_register_without_prewarm_does_not_init(self):
        init_fn = MagicMock()
        mgr = LazyPluginManager(init_fn=init_fn)
        mgr.register(_spec("alpha"))
        init_fn.assert_not_called()


class TestEnsureToolReady:
    def test_initializes_owning_plugin(self):
        init_fn = MagicMock()
        mgr = LazyPluginManager(init_fn=init_fn)
        spec = _spec("alpha", tools=(_tool("t1"),))
        mgr.register(spec)

        assert mgr.ensure_tool_ready("t1") is True
        init_fn.assert_called_once_with(spec)
        assert mgr.get_state("alpha") == LazyState.ACTIVE

    def test_increments_use_count_and_updates_last_used(self):
        mgr = LazyPluginManager(init_fn=MagicMock())
        spec = _spec("alpha", tools=(_tool("t1"),))
        mgr.register(spec)

        before = time.time()
        mgr.ensure_tool_ready("t1")
        mgr.ensure_tool_ready("t1")
        entry = mgr.all_entries()["alpha"]
        assert entry.use_count == 2
        assert entry.last_used_at >= before

    def test_returns_false_for_unknown_tool(self):
        mgr = LazyPluginManager(init_fn=MagicMock())
        assert mgr.ensure_tool_ready("no-such-tool") is False

    def test_failed_init_sets_failed_state_and_records_error(self):
        init_fn = MagicMock(side_effect=RuntimeError("boom"))
        mgr = LazyPluginManager(init_fn=init_fn)
        spec = _spec("alpha", tools=(_tool("t1"),))
        mgr.register(spec)

        assert mgr.ensure_tool_ready("t1") is False
        assert mgr.get_state("alpha") == LazyState.FAILED
        assert "boom" in mgr.all_entries()["alpha"].error

    def test_failed_plugin_returns_false_on_subsequent_calls(self):
        init_fn = MagicMock(side_effect=RuntimeError("boom"))
        mgr = LazyPluginManager(init_fn=init_fn)
        spec = _spec("alpha", tools=(_tool("t1"),))
        mgr.register(spec)

        mgr.ensure_tool_ready("t1")
        init_fn.reset_mock()
        assert mgr.ensure_tool_ready("t1") is False
        init_fn.assert_not_called()  # no retry

    def test_already_active_skips_init(self):
        init_fn = MagicMock()
        mgr = LazyPluginManager(init_fn=init_fn)
        spec = _spec("alpha", tools=(_tool("t1"),))
        mgr.register(spec)

        mgr.ensure_tool_ready("t1")
        init_fn.reset_mock()
        assert mgr.ensure_tool_ready("t1") is True
        init_fn.assert_not_called()


class TestSuspendResume:
    def test_suspend_active_returns_true(self):
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_spec("alpha"))
        mgr._ensure_initialized("alpha")
        assert mgr.suspend("alpha") is True
        assert mgr.get_state("alpha") == LazyState.SUSPENDED

    def test_suspend_non_active_returns_false(self):
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_spec("alpha"))
        assert mgr.suspend("alpha") is False

    def test_suspend_unknown_returns_false(self):
        mgr = LazyPluginManager(init_fn=MagicMock())
        assert mgr.suspend("nope") is False

    def test_resume_suspended_reinitializes(self):
        init_fn = MagicMock()
        mgr = LazyPluginManager(init_fn=init_fn)
        mgr.register(_spec("alpha"))
        mgr._ensure_initialized("alpha")
        mgr.suspend("alpha")

        assert mgr.resume("alpha") is True
        assert mgr.get_state("alpha") == LazyState.ACTIVE
        assert init_fn.call_count == 2  # initial + resume

    def test_resume_non_suspended_returns_false(self):
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_spec("alpha"))
        mgr._ensure_initialized("alpha")
        assert mgr.resume("alpha") is False

    def test_resume_unknown_returns_false(self):
        mgr = LazyPluginManager(init_fn=MagicMock())
        assert mgr.resume("nope") is False


class TestQueries:
    def test_get_state_returns_correct_states(self):
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_spec("a"))
        assert mgr.get_state("a") == LazyState.READY
        mgr._ensure_initialized("a")
        assert mgr.get_state("a") == LazyState.ACTIVE

    def test_get_state_returns_none_for_unknown(self):
        mgr = LazyPluginManager(init_fn=MagicMock())
        assert mgr.get_state("nope") is None

    def test_is_active_true_when_active(self):
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_spec("a"))
        mgr._ensure_initialized("a")
        assert mgr.is_active("a") is True

    def test_is_active_false_when_not_active(self):
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_spec("a"))
        assert mgr.is_active("a") is False

    def test_active_plugins_returns_only_active(self):
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_spec("a"))
        mgr.register(_spec("b"))
        mgr._ensure_initialized("a")
        assert mgr.active_plugins() == ["a"]

    def test_all_entries_returns_all(self):
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_spec("a"))
        mgr.register(_spec("b"))
        entries = mgr.all_entries()
        assert set(entries.keys()) == {"a", "b"}

    def test_all_entries_returns_copy(self):
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_spec("a"))
        entries = mgr.all_entries()
        entries["hacked"] = None  # type: ignore[assignment]
        assert "hacked" not in mgr.all_entries()

    def test_stats_counts_by_state(self):
        init_fn = MagicMock()
        mgr = LazyPluginManager(init_fn=init_fn)
        mgr.register(_spec("a"))
        mgr.register(_spec("b"))
        mgr.register(_spec("c"))
        mgr._ensure_initialized("a")
        init_fn.side_effect = RuntimeError("fail")
        mgr._ensure_initialized("b")
        stats = mgr.stats()
        assert stats["active"] == 1
        assert stats["failed"] == 1
        assert stats["ready"] == 1

    def test_multiple_plugins_separate_tool_mappings(self):
        mgr = LazyPluginManager(init_fn=MagicMock())
        mgr.register(_spec("alpha", tools=(_tool("t1"),)))
        mgr.register(_spec("beta", tools=(_tool("t2"),)))

        mgr.ensure_tool_ready("t1")
        assert mgr.is_active("alpha") is True
        assert mgr.is_active("beta") is False

        mgr.ensure_tool_ready("t2")
        assert mgr.is_active("beta") is True


# ---------------------------------------------------------------------------
# NativeAdapter
# ---------------------------------------------------------------------------


class TestNativeAdapter:
    def test_can_handle_native(self):
        adapter = NativeAdapter()
        assert adapter.can_handle(_spec(runtime="native")) is True

    def test_can_handle_rejects_non_native(self):
        adapter = NativeAdapter()
        assert adapter.can_handle(_spec(runtime="cli")) is False
        assert adapter.can_handle(_spec(runtime="content")) is False

    @pytest.mark.asyncio
    async def test_load_resolves_handler_refs(self):
        fake_fn = MagicMock()
        fake_mod = MagicMock()
        fake_mod.func = fake_fn

        spec = _spec(tools=(_tool("t1", "mymod:func"),))
        adapter = NativeAdapter()
        with patch("obscura.plugins.adapters.native.importlib.import_module", return_value=fake_mod):
            result = await adapter.load(spec, {})
        assert "t1" in result["handlers"]
        assert result["handlers"]["t1"] is fake_fn

    @pytest.mark.asyncio
    async def test_load_skips_tools_with_no_handler(self):
        spec = _spec(tools=(_tool_no_handler("empty"),))
        adapter = NativeAdapter()
        result = await adapter.load(spec, {})
        assert "empty" not in result["handlers"]

    @pytest.mark.asyncio
    async def test_load_handles_resolution_error_gracefully(self):
        spec = _spec(tools=(_tool("bad", "no.such:thing"),))
        adapter = NativeAdapter()
        with patch(
            "obscura.plugins.adapters.native.importlib.import_module",
            side_effect=ModuleNotFoundError("nope"),
        ):
            result = await adapter.load(spec, {})
        assert "bad" not in result["handlers"]

    @pytest.mark.asyncio
    async def test_healthcheck_callable_type(self):
        hc = HealthcheckSpec(type="callable", target="mymod:check")
        spec = _spec(healthcheck=hc)
        adapter = NativeAdapter()
        fake_fn = MagicMock(return_value=True)
        fake_mod = MagicMock()
        fake_mod.check = fake_fn
        with patch("obscura.plugins.adapters.native.importlib.import_module", return_value=fake_mod):
            assert await adapter.healthcheck(spec) is True
        fake_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_healthcheck_callable_failure_returns_false(self):
        hc = HealthcheckSpec(type="callable", target="mymod:check")
        spec = _spec(healthcheck=hc)
        adapter = NativeAdapter()
        with patch(
            "obscura.plugins.adapters.native.importlib.import_module",
            side_effect=Exception("boom"),
        ):
            assert await adapter.healthcheck(spec) is False

    @pytest.mark.asyncio
    async def test_healthcheck_no_spec_returns_true(self):
        spec = _spec(healthcheck=None)
        adapter = NativeAdapter()
        assert await adapter.healthcheck(spec) is True

    @pytest.mark.asyncio
    async def test_teardown_is_noop(self):
        adapter = NativeAdapter()
        await adapter.teardown(_spec())  # should not raise


class TestResolveHandler:
    def test_colon_syntax(self):
        fake_mod = MagicMock()
        fake_mod.my_func = lambda: None
        with patch("obscura.plugins.adapters.native.importlib.import_module", return_value=fake_mod) as mock_imp:
            result = _resolve_handler("some.module:my_func")
        mock_imp.assert_called_once_with("some.module")
        assert result is fake_mod.my_func

    def test_dot_syntax_fallback(self):
        fake_mod = MagicMock()
        fake_mod.my_func = lambda: None
        with patch("obscura.plugins.adapters.native.importlib.import_module", return_value=fake_mod) as mock_imp:
            result = _resolve_handler("some.module.my_func")
        mock_imp.assert_called_once_with("some.module")
        assert result is fake_mod.my_func


# ---------------------------------------------------------------------------
# CLIAdapter
# ---------------------------------------------------------------------------


class TestCLIAdapter:
    def test_can_handle_cli(self):
        adapter = CLIAdapter()
        assert adapter.can_handle(_spec(runtime="cli")) is True

    def test_can_handle_rejects_non_cli(self):
        adapter = CLIAdapter()
        assert adapter.can_handle(_spec(runtime="native")) is False

    @pytest.mark.asyncio
    async def test_load_creates_handlers_for_tools(self):
        spec = _spec(runtime="cli", tools=(_tool("grep-tool", "grep {pattern}"),))
        adapter = CLIAdapter()
        result = await adapter.load(spec, {})
        assert "grep-tool" in result["handlers"]
        assert callable(result["handlers"]["grep-tool"])

    @pytest.mark.asyncio
    async def test_load_skips_tools_without_handler(self):
        spec = _spec(runtime="cli", tools=(_tool_no_handler("empty"),))
        adapter = CLIAdapter()
        result = await adapter.load(spec, {})
        assert "empty" not in result["handlers"]

    @pytest.mark.asyncio
    async def test_healthcheck_checks_shutil_which(self):
        hc = HealthcheckSpec(type="binary", target="rg")
        spec = _spec(runtime="cli", healthcheck=hc)
        adapter = CLIAdapter()
        with patch("obscura.plugins.adapters.cli.shutil.which", return_value="/usr/bin/rg"):
            assert await adapter.healthcheck(spec) is True
        with patch("obscura.plugins.adapters.cli.shutil.which", return_value=None):
            assert await adapter.healthcheck(spec) is False

    @pytest.mark.asyncio
    async def test_healthcheck_no_spec_checks_first_tool_binary(self):
        spec = _spec(runtime="cli", tools=(_tool("t", "mytool --flag"),))
        adapter = CLIAdapter()
        with patch("obscura.plugins.adapters.cli.shutil.which", return_value="/usr/bin/mytool") as mock_w:
            assert await adapter.healthcheck(spec) is True
            mock_w.assert_called_with("mytool")

    @pytest.mark.asyncio
    async def test_healthcheck_no_spec_no_tools_returns_true(self):
        spec = _spec(runtime="cli", tools=())
        adapter = CLIAdapter()
        assert await adapter.healthcheck(spec) is True


class TestCLIHandler:
    @pytest.mark.asyncio
    async def test_cli_handler_success(self):
        handler = _make_cli_handler("echo {msg}", "echo-tool")
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"hello\n", b"")
        mock_proc.returncode = 0

        with patch("obscura.plugins.adapters.cli.asyncio.create_subprocess_shell", return_value=mock_proc):
            result = await handler(msg="hello")
        assert result == "hello\n"

    @pytest.mark.asyncio
    async def test_cli_handler_failure_raises(self):
        handler = _make_cli_handler("false", "fail-tool")
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"error msg")
        mock_proc.returncode = 1

        with patch("obscura.plugins.adapters.cli.asyncio.create_subprocess_shell", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="fail-tool failed"):
                await handler()

    @pytest.mark.asyncio
    async def test_cli_handler_substitutes_params(self):
        handler = _make_cli_handler("grep {pattern} {file}", "grep-tool")
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"match", b"")
        mock_proc.returncode = 0

        with patch("obscura.plugins.adapters.cli.asyncio.create_subprocess_shell", return_value=mock_proc) as mock_sub:
            await handler(pattern="foo", file="bar.txt")
        mock_sub.assert_called_once()
        cmd = mock_sub.call_args[0][0]
        assert "foo" in cmd
        assert "bar.txt" in cmd


# ---------------------------------------------------------------------------
# ContentAdapter
# ---------------------------------------------------------------------------


class TestContentAdapter:
    def test_can_handle_content(self):
        adapter = ContentAdapter()
        assert adapter.can_handle(_spec(runtime="content")) is True

    def test_can_handle_rejects_non_content(self):
        adapter = ContentAdapter()
        assert adapter.can_handle(_spec(runtime="native")) is False

    @pytest.mark.asyncio
    async def test_load_returns_empty_handlers(self):
        adapter = ContentAdapter()
        result = await adapter.load(_spec(runtime="content"), {})
        assert result == {"handlers": {}}

    @pytest.mark.asyncio
    async def test_healthcheck_always_true(self):
        adapter = ContentAdapter()
        assert await adapter.healthcheck(_spec(runtime="content")) is True

    @pytest.mark.asyncio
    async def test_teardown_is_noop(self):
        adapter = ContentAdapter()
        await adapter.teardown(_spec(runtime="content"))  # should not raise


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_native_adapter_satisfies_protocol(self):
        assert isinstance(NativeAdapter(), PluginAdapter)

    def test_cli_adapter_satisfies_protocol(self):
        assert isinstance(CLIAdapter(), PluginAdapter)

    def test_content_adapter_satisfies_protocol(self):
        assert isinstance(ContentAdapter(), PluginAdapter)
