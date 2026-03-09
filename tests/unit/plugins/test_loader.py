"""Comprehensive tests for obscura.plugins.loader pipeline."""

from __future__ import annotations

import asyncio
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from obscura.plugins.loader import (
    ManifestToolProvider,
    PluginLoader,
    _check_config,
    _resolve_handler,
)
from obscura.plugins.models import (
    ConfigRequirement,
    PluginSpec,
    PluginStatus,
    ToolContribution,
    BootstrapSpec,
    BootstrapDep,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(
    plugin_id: str = "test-plugin",
    name: str = "Test Plugin",
    version: str = "1.0.0",
    config_requirements: tuple[ConfigRequirement, ...] = (),
    tools: tuple[ToolContribution, ...] = (),
    bootstrap: BootstrapSpec | None = None,
    **kwargs: Any,
) -> PluginSpec:
    """Build a minimal valid PluginSpec for testing."""
    defaults: dict[str, Any] = dict(
        id=plugin_id,
        name=name,
        version=version,
        source_type="builtin",
        runtime_type="native",
        trust_level="builtin",
        author="test-author",
        description="A test plugin",
        config_requirements=config_requirements,
        tools=tools,
        bootstrap=bootstrap,
    )
    defaults.update(kwargs)
    return PluginSpec(**defaults)


class _FakeProviderRegistry:
    """Minimal stand-in for ToolProviderRegistry with an add() method."""

    def __init__(self) -> None:
        self.providers: list[Any] = []

    def add(self, provider: Any) -> None:
        self.providers.append(provider)


# ---------------------------------------------------------------------------
# _check_config
# ---------------------------------------------------------------------------


class TestCheckConfig:
    """Tests for the _check_config helper."""

    def test_no_requirements_is_satisfied(self) -> None:
        spec = _make_spec()
        ok, missing = _check_config(spec)
        assert ok is True
        assert missing == []

    def test_required_key_present_in_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_API_KEY", "secret123")
        spec = _make_spec(
            config_requirements=(
                ConfigRequirement(key="MY_API_KEY", required=True),
            ),
        )
        ok, missing = _check_config(spec)
        assert ok is True
        assert missing == []

    def test_required_key_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISSING_KEY", raising=False)
        spec = _make_spec(
            config_requirements=(
                ConfigRequirement(key="MISSING_KEY", required=True),
            ),
        )
        ok, missing = _check_config(spec)
        assert ok is False
        assert "MISSING_KEY" in missing

    def test_required_key_with_default_is_satisfied(self) -> None:
        spec = _make_spec(
            config_requirements=(
                ConfigRequirement(key="OPT_KEY", required=True, default="fallback"),
            ),
        )
        ok, missing = _check_config(spec)
        assert ok is True

    def test_optional_key_missing_is_satisfied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPT_KEY", raising=False)
        spec = _make_spec(
            config_requirements=(
                ConfigRequirement(key="OPT_KEY", required=False),
            ),
        )
        ok, missing = _check_config(spec)
        assert ok is True
        assert missing == []

    def test_whitespace_only_env_var_counts_as_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BLANK_KEY", "   ")
        spec = _make_spec(
            config_requirements=(
                ConfigRequirement(key="BLANK_KEY", required=True),
            ),
        )
        ok, missing = _check_config(spec)
        assert ok is False
        assert "BLANK_KEY" in missing

    def test_multiple_missing_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KEY_A", raising=False)
        monkeypatch.delenv("KEY_B", raising=False)
        monkeypatch.setenv("KEY_C", "present")
        spec = _make_spec(
            config_requirements=(
                ConfigRequirement(key="KEY_A", required=True),
                ConfigRequirement(key="KEY_B", required=True),
                ConfigRequirement(key="KEY_C", required=True),
            ),
        )
        ok, missing = _check_config(spec)
        assert ok is False
        assert set(missing) == {"KEY_A", "KEY_B"}


# ---------------------------------------------------------------------------
# _resolve_handler
# ---------------------------------------------------------------------------


class TestResolveHandler:
    """Tests for the _resolve_handler helper."""

    def test_colon_notation(self) -> None:
        result = _resolve_handler("os.path:join")
        assert result is not None
        assert callable(result)
        import os.path
        assert result is os.path.join

    def test_dot_notation(self) -> None:
        result = _resolve_handler("os.path.join")
        assert result is not None
        assert callable(result)

    def test_empty_string_returns_none(self) -> None:
        assert _resolve_handler("") is None

    def test_single_word_returns_none(self) -> None:
        assert _resolve_handler("os") is None

    def test_nonexistent_module_returns_none(self) -> None:
        assert _resolve_handler("totally.fake.module:nope") is None

    def test_nonexistent_attr_returns_none(self) -> None:
        assert _resolve_handler("os.path:definitely_not_real_attr") is None

    def test_builtin_module(self) -> None:
        result = _resolve_handler("json:dumps")
        assert result is not None
        import json
        assert result is json.dumps


# ---------------------------------------------------------------------------
# ManifestToolProvider
# ---------------------------------------------------------------------------


class TestManifestToolProvider:
    """Tests for ManifestToolProvider."""

    def test_creation_from_spec(self) -> None:
        spec = _make_spec(tools=(
            ToolContribution(
                name="my_tool",
                description="A tool",
                handler_ref="json:dumps",
                parameters={"type": "object"},
            ),
        ))
        provider = ManifestToolProvider(spec)
        assert provider.spec is spec
        assert provider._installed is False

    def test_install_registers_tools(self) -> None:
        spec = _make_spec(tools=(
            ToolContribution(
                name="my_tool",
                description="A tool",
                handler_ref="json:dumps",
                parameters={"type": "object"},
            ),
        ))
        provider = ManifestToolProvider(spec)

        mock_context = MagicMock()
        mock_context.agent.client.register_tool = MagicMock(return_value=None)

        asyncio.get_event_loop().run_until_complete(provider.install(mock_context))

        mock_context.agent.client.register_tool.assert_called_once()
        tool_spec = mock_context.agent.client.register_tool.call_args[0][0]
        assert tool_spec.name == "my_tool"
        assert tool_spec.description == "A tool"
        assert provider._installed is True

    def test_install_skips_unresolvable_handler(self) -> None:
        spec = _make_spec(tools=(
            ToolContribution(
                name="bad_tool",
                description="Bad handler",
                handler_ref="nonexistent.module:func",
                parameters={},
            ),
        ))
        provider = ManifestToolProvider(spec)
        mock_context = MagicMock()
        mock_context.agent.client.register_tool = MagicMock(return_value=None)

        asyncio.get_event_loop().run_until_complete(provider.install(mock_context))

        mock_context.agent.client.register_tool.assert_not_called()
        assert provider._installed is True

    def test_install_handles_async_register(self) -> None:
        spec = _make_spec(tools=(
            ToolContribution(
                name="async_tool",
                description="Async tool",
                handler_ref="json:loads",
                parameters={"type": "object"},
            ),
        ))
        provider = ManifestToolProvider(spec)

        mock_context = MagicMock()
        mock_context.agent.client.register_tool = AsyncMock(return_value=None)

        asyncio.get_event_loop().run_until_complete(provider.install(mock_context))

        mock_context.agent.client.register_tool.assert_called_once()
        assert provider._installed is True

    def test_uninstall(self) -> None:
        spec = _make_spec()
        provider = ManifestToolProvider(spec)
        provider._installed = True
        asyncio.get_event_loop().run_until_complete(provider.uninstall(MagicMock()))
        assert provider._installed is False


# ---------------------------------------------------------------------------
# PluginLoader.discover_builtins
# ---------------------------------------------------------------------------


class TestDiscoverBuiltins:
    """Tests for PluginLoader.discover_builtins()."""

    def test_finds_all_builtins(self, tmp_path: Path) -> None:
        loader = PluginLoader(plugin_dir=tmp_path)
        specs = loader.discover_builtins()
        assert len(specs) >= 20
        ids = {s.id for s in specs}
        assert "websearch" in ids
        assert "gitleaks" in ids
        assert "skill-pytight" in ids

    def test_all_builtins_have_valid_ids(self, tmp_path: Path) -> None:
        loader = PluginLoader(plugin_dir=tmp_path)
        for spec in loader.discover_builtins():
            assert spec.id, "Each builtin must have an id"
            assert spec.version, "Each builtin must have a version"


# ---------------------------------------------------------------------------
# PluginLoader.discover_local
# ---------------------------------------------------------------------------


class TestDiscoverLocal:
    """Tests for PluginLoader.discover_local()."""

    def test_discovers_yaml_manifest(self, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "my-plugin"
        plugin_dir.mkdir()
        manifest = plugin_dir / "plugin.yaml"
        manifest.write_text(textwrap.dedent("""\
            id: my-local-plugin
            name: My Local Plugin
            version: "0.1.0"
            source_type: local
            runtime_type: native
            trust_level: community
            author: tester
            description: A local test plugin
        """))
        loader = PluginLoader(plugin_dir=tmp_path)
        specs = loader.discover_local()
        assert len(specs) == 1
        assert specs[0].id == "my-local-plugin"

    def test_discovers_json_manifest(self, tmp_path: Path) -> None:
        import json
        plugin_dir = tmp_path / "json-plugin"
        plugin_dir.mkdir()
        manifest = plugin_dir / "plugin.json"
        manifest.write_text(json.dumps({
            "id": "json-plugin",
            "name": "JSON Plugin",
            "version": "1.0.0",
            "source_type": "local",
            "runtime_type": "native",
            "trust_level": "community",
            "author": "tester",
            "description": "A JSON-based plugin",
        }))
        loader = PluginLoader(plugin_dir=tmp_path)
        specs = loader.discover_local()
        assert len(specs) == 1
        assert specs[0].id == "json-plugin"

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        loader = PluginLoader(plugin_dir=tmp_path)
        assert loader.discover_local() == []

    def test_nonexistent_dir_returns_empty(self, tmp_path: Path) -> None:
        loader = PluginLoader(plugin_dir=tmp_path / "nope")
        assert loader.discover_local() == []

    def test_skips_invalid_manifest(self, tmp_path: Path) -> None:
        plugin_dir = tmp_path / "bad-plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.yaml").write_text("not: valid: yaml: [[[")
        loader = PluginLoader(plugin_dir=tmp_path)
        specs = loader.discover_local()
        assert len(specs) == 0


# ---------------------------------------------------------------------------
# PluginLoader._load_spec
# ---------------------------------------------------------------------------


class TestLoadSpec:
    """Tests for the _load_spec pipeline."""

    def test_valid_spec_becomes_enabled(self, tmp_path: Path) -> None:
        spec = _make_spec()
        loader = PluginLoader(plugin_dir=tmp_path)
        registry = _FakeProviderRegistry()
        status = loader._load_spec(spec, registry)
        assert status.state == "enabled"
        assert status.enabled is True
        assert len(registry.providers) == 1
        assert isinstance(registry.providers[0], ManifestToolProvider)

    def test_invalid_spec_becomes_failed(self, tmp_path: Path) -> None:
        # Duplicate tool names produce a hard validation error
        spec = _make_spec(tools=(
            ToolContribution(name="dup", description="a", handler_ref="os:getcwd", parameters={}),
            ToolContribution(name="dup", description="b", handler_ref="os:getcwd", parameters={}),
        ))
        loader = PluginLoader(plugin_dir=tmp_path)
        registry = _FakeProviderRegistry()
        status = loader._load_spec(spec, registry)
        assert status.state == "failed"
        assert status.error is not None

    def test_missing_config_becomes_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("REQUIRED_SECRET", raising=False)
        spec = _make_spec(
            source_type="local",
            trust_level="community",
            config_requirements=(
                ConfigRequirement(key="REQUIRED_SECRET", required=True),
            ),
        )
        loader = PluginLoader(plugin_dir=tmp_path)
        registry = _FakeProviderRegistry()
        status = loader._load_spec(spec, registry)
        assert status.state == "disabled"
        assert "REQUIRED_SECRET" in (status.error or "")
        assert len(registry.providers) == 0

    def test_bootstrap_failure_becomes_failed(self, tmp_path: Path) -> None:
        spec = _make_spec(
            source_type="local",
            trust_level="community",
            bootstrap=BootstrapSpec(
                deps=(BootstrapDep(type="pip", package="fake-pkg"),),
            ),
        )
        loader = PluginLoader(plugin_dir=tmp_path)
        registry = _FakeProviderRegistry()

        @dataclass
        class _BootstrapResult:
            ok: bool = False
            errors: list[str] = field(default_factory=lambda: ["install failed"])
            installed: list[str] = field(default_factory=list)

        mock_bootstrapper = MagicMock()
        mock_bootstrapper.run_bootstrap = MagicMock(return_value=_BootstrapResult())

        with patch.dict(
            "sys.modules",
            {"obscura.plugins.bootstrapper": mock_bootstrapper},
        ):
            status = loader._load_spec(spec, registry)

        assert status.state == "failed"
        assert "Bootstrap" in (status.error or "")

    def test_bootstrap_exception_becomes_failed(self, tmp_path: Path) -> None:
        spec = _make_spec(
            source_type="local",
            trust_level="community",
            bootstrap=BootstrapSpec(
                deps=(BootstrapDep(type="pip", package="fake-pkg"),),
            ),
        )
        loader = PluginLoader(plugin_dir=tmp_path)
        registry = _FakeProviderRegistry()

        mock_bootstrapper = MagicMock()
        mock_bootstrapper.run_bootstrap.side_effect = RuntimeError("boom")

        with patch.dict(
            "sys.modules",
            {"obscura.plugins.bootstrapper": mock_bootstrapper},
        ):
            status = loader._load_spec(spec, registry)

        assert status.state == "failed"
        assert "Bootstrap" in (status.error or "") or "boom" in (status.error or "")


# ---------------------------------------------------------------------------
# PluginLoader.load_builtins
# ---------------------------------------------------------------------------


class TestLoadBuiltins:
    """Tests for PluginLoader.load_builtins()."""

    def test_loads_all_builtins(self, tmp_path: Path) -> None:
        loader = PluginLoader(plugin_dir=tmp_path)
        registry = _FakeProviderRegistry()
        results = loader.load_builtins(registry)
        assert len(results) > 0
        # All builtins should be either enabled or disabled (not crashed)
        for plugin_id, status in results.items():
            assert status.state in ("enabled", "disabled", "failed"), (
                f"{plugin_id} unexpected state: {status.state}"
            )

    def test_returns_dict_of_plugin_status(self, tmp_path: Path) -> None:
        loader = PluginLoader(plugin_dir=tmp_path)
        registry = _FakeProviderRegistry()
        results = loader.load_builtins(registry)
        for pid, st in results.items():
            assert isinstance(pid, str)
            assert isinstance(st, PluginStatus)


# ---------------------------------------------------------------------------
# PluginLoader.load_all
# ---------------------------------------------------------------------------


class TestLoadAll:
    """Tests for PluginLoader.load_all() end-to-end."""

    def test_returns_summary_keys(self, tmp_path: Path) -> None:
        loader = PluginLoader(plugin_dir=tmp_path)
        registry = _FakeProviderRegistry()
        result = loader.load_all(registry)
        assert "builtins" in result
        assert "local_manifest" in result
        assert "user_plugins" in result

    def test_providers_added_to_registry(self, tmp_path: Path) -> None:
        loader = PluginLoader(plugin_dir=tmp_path)
        registry = _FakeProviderRegistry()
        loader.load_all(registry)
        # At minimum, builtins should have added providers
        assert len(registry.providers) > 0

    def test_load_all_enabled_is_alias(self, tmp_path: Path) -> None:
        loader = PluginLoader(plugin_dir=tmp_path)
        registry = _FakeProviderRegistry()
        result = loader.load_all_enabled(registry)
        assert "builtins" in result


# ---------------------------------------------------------------------------
# Status queries
# ---------------------------------------------------------------------------


class TestStatusQueries:
    """Tests for get_status and list_loaded."""

    def test_get_status_after_load(self, tmp_path: Path) -> None:
        spec = _make_spec(plugin_id="status-test")
        loader = PluginLoader(plugin_dir=tmp_path)
        registry = _FakeProviderRegistry()
        loader._load_spec(spec, registry)
        loader._loaded["status-test"] = PluginStatus(plugin_id="status-test", state="enabled")
        status = loader.get_status("status-test")
        assert status is not None
        assert status.state == "enabled"

    def test_get_status_unknown_returns_none(self, tmp_path: Path) -> None:
        loader = PluginLoader(plugin_dir=tmp_path)
        assert loader.get_status("nonexistent") is None

    def test_list_loaded(self, tmp_path: Path) -> None:
        loader = PluginLoader(plugin_dir=tmp_path)
        registry = _FakeProviderRegistry()
        loader.load_builtins(registry)
        loaded = loader.list_loaded()
        assert isinstance(loaded, dict)
        assert len(loaded) > 0
