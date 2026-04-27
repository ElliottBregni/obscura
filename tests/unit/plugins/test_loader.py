"""Comprehensive tests for obscura.plugins.loader pipeline."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

from obscura.plugins.loader import (
    PluginLoader,
    _check_config,
    _resolve_handler,
)
from obscura.plugins.models import (
    BootstrapDep,
    BootstrapSpec,
    ConfigRequirement,
    PluginSpec,
    PluginStatus,
    ToolContribution,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

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
    defaults: dict[str, Any] = {
        "id": plugin_id,
        "name": name,
        "version": version,
        "source_type": "builtin",
        "runtime_type": "native",
        "trust_level": "builtin",
        "author": "test-author",
        "description": "A test plugin",
        "config_requirements": config_requirements,
        "tools": tools,
        "bootstrap": bootstrap,
    }
    defaults.update(kwargs)
    return PluginSpec(**defaults)


class _FakeBroker:
    """Minimal stand-in for ToolBroker with a register_tool_spec() method."""

    def __init__(self) -> None:
        self.registered: list[Any] = []

    def register_tool_spec(self, spec: Any) -> None:
        self.registered.append(spec)


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
            config_requirements=(ConfigRequirement(key="MY_API_KEY", required=True),),
        )
        ok, missing = _check_config(spec)
        assert ok is True
        assert missing == []

    def test_required_key_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISSING_KEY", raising=False)
        spec = _make_spec(
            config_requirements=(ConfigRequirement(key="MISSING_KEY", required=True),),
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
        ok, _missing = _check_config(spec)
        assert ok is True

    def test_optional_key_missing_is_satisfied(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OPT_KEY", raising=False)
        spec = _make_spec(
            config_requirements=(ConfigRequirement(key="OPT_KEY", required=False),),
        )
        ok, missing = _check_config(spec)
        assert ok is True
        assert missing == []

    def test_whitespace_only_env_var_counts_as_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BLANK_KEY", "   ")
        spec = _make_spec(
            config_requirements=(ConfigRequirement(key="BLANK_KEY", required=True),),
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


class TestResolveHandlerTrustGate:
    """SOC2 finding B1 — non-builtin plugins may only resolve obscura.* handlers.

    Pre-fix, a manifest dropped into a project's ``.obscura/plugins/`` could
    declare ``handler = "os:system"`` and hand the LLM a free shell. Post-fix,
    only the trusted module prefixes (``obscura`` by default) are imported
    when *plugin_spec* is supplied and is not source_type=="builtin".
    """

    def test_local_plugin_blocked_from_stdlib(self) -> None:
        spec = _make_spec(plugin_id="evil", source_type="local")
        assert _resolve_handler("os:system", plugin_spec=spec) is None

    def test_local_plugin_blocked_from_subprocess(self) -> None:
        spec = _make_spec(plugin_id="evil", source_type="local")
        assert _resolve_handler("subprocess:Popen", plugin_spec=spec) is None

    def test_local_plugin_allowed_to_resolve_obscura(self) -> None:
        spec = _make_spec(plugin_id="legit", source_type="local")
        result = _resolve_handler(
            "obscura.tools.system:_resolve_base_dir",
            plugin_spec=spec,
        )
        assert result is not None
        assert callable(result)

    def test_builtin_source_type_unrestricted(self) -> None:
        spec = _make_spec(plugin_id="builtin-x", source_type="builtin")
        result = _resolve_handler("json:dumps", plugin_spec=spec)
        import json

        assert result is json.dumps

    def test_git_sourced_plugin_blocked_from_stdlib(self) -> None:
        spec = _make_spec(plugin_id="from-github", source_type="git")
        assert _resolve_handler("os:system", plugin_spec=spec) is None

    def test_no_spec_skips_check_for_back_compat(self) -> None:
        result = _resolve_handler("json:dumps")
        import json

        assert result is json.dumps

    def test_extra_trusted_prefix_via_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(
            "OBSCURA_PLUGIN_TRUSTED_HANDLER_PREFIXES",
            "json,xml",
        )
        spec = _make_spec(plugin_id="ops-allowed", source_type="local")
        result = _resolve_handler("json:dumps", plugin_spec=spec)
        import json

        assert result is json.dumps


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
        manifest.write_text(
            textwrap.dedent("""\
            id: my-local-plugin
            name: My Local Plugin
            version: "0.1.0"
            source_type: local
            runtime_type: native
            trust_level: community
            author: tester
            description: A local test plugin
        """),
        )
        loader = PluginLoader(plugin_dir=tmp_path)
        specs = loader.discover_local()
        assert len(specs) == 1
        assert specs[0].id == "my-local-plugin"

    def test_discovers_json_manifest(self, tmp_path: Path) -> None:
        import json

        plugin_dir = tmp_path / "json-plugin"
        plugin_dir.mkdir()
        manifest = plugin_dir / "plugin.json"
        manifest.write_text(
            json.dumps(
                {
                    "id": "json-plugin",
                    "name": "JSON Plugin",
                    "version": "1.0.0",
                    "source_type": "local",
                    "runtime_type": "native",
                    "trust_level": "community",
                    "author": "tester",
                    "description": "A JSON-based plugin",
                },
            ),
        )
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
        broker = _FakeBroker()
        status = loader._load_spec(spec, broker)
        assert status.state == "enabled"
        assert status.enabled is True

    def test_valid_spec_with_tools_registers_on_broker(self, tmp_path: Path) -> None:
        spec = _make_spec(
            tools=(
                ToolContribution(
                    name="my_tool",
                    description="A tool",
                    handler_ref="json:dumps",
                    parameters={"type": "object"},
                ),
            ),
        )
        loader = PluginLoader(plugin_dir=tmp_path)
        broker = _FakeBroker()
        status = loader._load_spec(spec, broker)
        assert status.state == "enabled"
        assert len(broker.registered) == 1
        assert broker.registered[0].name == "my_tool"

    def test_invalid_spec_becomes_failed(self, tmp_path: Path) -> None:
        # Duplicate tool names produce a hard validation error
        spec = _make_spec(
            tools=(
                ToolContribution(
                    name="dup",
                    description="a",
                    handler_ref="os:getcwd",
                    parameters={},
                ),
                ToolContribution(
                    name="dup",
                    description="b",
                    handler_ref="os:getcwd",
                    parameters={},
                ),
            ),
        )
        loader = PluginLoader(plugin_dir=tmp_path)
        broker = _FakeBroker()
        status = loader._load_spec(spec, broker)
        assert status.state == "failed"
        assert status.error is not None

    def test_missing_config_becomes_disabled(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
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
        broker = _FakeBroker()
        status = loader._load_spec(spec, broker)
        assert status.state == "disabled"
        assert "REQUIRED_SECRET" in (status.error or "")
        assert len(broker.registered) == 0

    def test_bootstrap_failure_becomes_failed(self, tmp_path: Path) -> None:
        spec = _make_spec(
            source_type="local",
            trust_level="community",
            bootstrap=BootstrapSpec(
                deps=(BootstrapDep(type="pip", package="fake-pkg"),),
            ),
        )
        loader = PluginLoader(plugin_dir=tmp_path)
        broker = _FakeBroker()

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
            status = loader._load_spec(spec, broker)

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
        broker = _FakeBroker()

        mock_bootstrapper = MagicMock()
        mock_bootstrapper.run_bootstrap.side_effect = RuntimeError("boom")

        with patch.dict(
            "sys.modules",
            {"obscura.plugins.bootstrapper": mock_bootstrapper},
        ):
            status = loader._load_spec(spec, broker)

        assert status.state == "failed"
        assert "Bootstrap" in (status.error or "") or "boom" in (status.error or "")


# ---------------------------------------------------------------------------
# PluginLoader.load_builtins
# ---------------------------------------------------------------------------


class TestLoadBuiltins:
    """Tests for PluginLoader.load_builtins()."""

    def test_loads_all_builtins(self, tmp_path: Path) -> None:
        loader = PluginLoader(plugin_dir=tmp_path)
        broker = _FakeBroker()
        results = loader.load_builtins(broker)
        assert len(results) > 0
        # All builtins should be either enabled or disabled (not crashed)
        for plugin_id, status in results.items():
            assert status.state in ("enabled", "disabled", "failed"), (
                f"{plugin_id} unexpected state: {status.state}"
            )

    def test_returns_dict_of_plugin_status(self, tmp_path: Path) -> None:
        loader = PluginLoader(plugin_dir=tmp_path)
        broker = _FakeBroker()
        results = loader.load_builtins(broker)
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
        broker = _FakeBroker()
        result = loader.load_all(broker)
        assert "builtins" in result
        assert "local_manifest" in result
        assert "user_plugins" in result

    def test_load_all_enabled_is_alias(self, tmp_path: Path) -> None:
        loader = PluginLoader(plugin_dir=tmp_path)
        broker = _FakeBroker()
        result = loader.load_all_enabled(broker)
        assert "builtins" in result


# ---------------------------------------------------------------------------
# Status queries
# ---------------------------------------------------------------------------


class TestStatusQueries:
    """Tests for get_status and list_loaded."""

    def test_get_status_after_load(self, tmp_path: Path) -> None:
        spec = _make_spec(plugin_id="status-test")
        loader = PluginLoader(plugin_dir=tmp_path)
        broker = _FakeBroker()
        loader._load_spec(spec, broker)
        loader._loaded["status-test"] = PluginStatus(
            plugin_id="status-test",
            state="enabled",
        )
        status = loader.get_status("status-test")
        assert status is not None
        assert status.state == "enabled"

    def test_get_status_unknown_returns_none(self, tmp_path: Path) -> None:
        loader = PluginLoader(plugin_dir=tmp_path)
        assert loader.get_status("nonexistent") is None

    def test_list_loaded(self, tmp_path: Path) -> None:
        loader = PluginLoader(plugin_dir=tmp_path)
        broker = _FakeBroker()
        loader.load_builtins(broker)
        loaded = loader.list_loaded()
        assert isinstance(loaded, dict)
        assert len(loaded) > 0
