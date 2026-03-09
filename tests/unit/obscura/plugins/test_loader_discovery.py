"""Tests for PluginLoader multi-directory discovery (local, user, flat YAML)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from obscura.plugins.loader import PluginLoader


@pytest.fixture()
def _fake_global_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Set OBSCURA_HOME to a temp dir to isolate global discovery."""
    global_home = tmp_path / "global_home"
    global_home.mkdir()
    monkeypatch.setenv("OBSCURA_HOME", str(global_home))
    return global_home


_MINIMAL_MANIFEST = textwrap.dedent("""\
    id: {plugin_id}
    name: {name}
    version: "1.0.0"
    source_type: local
    runtime_type: content
    trust_level: community
    author: test
    description: Test plugin
""")


class TestDiscoverFromDir:
    """Tests for PluginLoader._discover_from_dir() static method."""

    def test_nonexistent_dir_returns_empty(self, tmp_path: Path) -> None:
        result = PluginLoader._discover_from_dir(tmp_path / "nope")
        assert result == []

    def test_empty_dir_returns_empty(self, tmp_path: Path) -> None:
        result = PluginLoader._discover_from_dir(tmp_path)
        assert result == []

    def test_flat_yaml_manifest(self, tmp_path: Path) -> None:
        """Flat *.yaml files (like builtins) should be discovered."""
        manifest = tmp_path / "my-tool.yaml"
        manifest.write_text(
            _MINIMAL_MANIFEST.format(plugin_id="my-tool", name="My Tool"),
            encoding="utf-8",
        )
        result = PluginLoader._discover_from_dir(tmp_path)
        assert len(result) == 1
        assert result[0].id == "my-tool"

    def test_subdir_manifest(self, tmp_path: Path) -> None:
        """Subdirectory with plugin.yaml should be discovered."""
        plugin_dir = tmp_path / "my-plugin"
        plugin_dir.mkdir()
        manifest = plugin_dir / "plugin.yaml"
        manifest.write_text(
            _MINIMAL_MANIFEST.format(plugin_id="my-plugin", name="My Plugin"),
            encoding="utf-8",
        )
        result = PluginLoader._discover_from_dir(tmp_path)
        assert len(result) == 1
        assert result[0].id == "my-plugin"

    def test_mixed_flat_and_subdir(self, tmp_path: Path) -> None:
        """Both flat YAML and subdirectory manifests in same dir."""
        # Flat
        (tmp_path / "flat-plugin.yaml").write_text(
            _MINIMAL_MANIFEST.format(plugin_id="flat-plugin", name="Flat"),
            encoding="utf-8",
        )
        # Subdir
        subdir = tmp_path / "sub-plugin"
        subdir.mkdir()
        (subdir / "plugin.yaml").write_text(
            _MINIMAL_MANIFEST.format(plugin_id="sub-plugin", name="Sub"),
            encoding="utf-8",
        )
        result = PluginLoader._discover_from_dir(tmp_path)
        ids = {s.id for s in result}
        assert ids == {"flat-plugin", "sub-plugin"}

    def test_registry_json_skipped(self, tmp_path: Path) -> None:
        """registry.json should not be parsed as a manifest."""
        (tmp_path / "registry.json").write_text("{}", encoding="utf-8")
        result = PluginLoader._discover_from_dir(tmp_path)
        assert result == []

    def test_invalid_yaml_skipped(self, tmp_path: Path) -> None:
        """Invalid YAML files should be skipped with a debug log."""
        (tmp_path / "broken.yaml").write_text("not: [valid: manifest", encoding="utf-8")
        result = PluginLoader._discover_from_dir(tmp_path)
        assert result == []


class TestDiscoverUser:
    """Tests for PluginLoader.discover_user()."""

    def test_no_user_plugins_when_same_dir(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """When plugin_dir IS the global dir, discover_user returns empty."""
        global_home = tmp_path / "home"
        global_plugins = global_home / "plugins"
        global_plugins.mkdir(parents=True)
        monkeypatch.setenv("OBSCURA_HOME", str(global_home))

        loader = PluginLoader(plugin_dir=global_plugins)
        assert loader.discover_user() == []

    def test_user_plugins_from_global(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """When local dir differs from global, discover_user finds global plugins."""
        global_home = tmp_path / "global"
        global_plugins = global_home / "plugins"
        global_plugins.mkdir(parents=True)
        monkeypatch.setenv("OBSCURA_HOME", str(global_home))

        # Write a flat YAML manifest in global plugins
        (global_plugins / "user-tool.yaml").write_text(
            _MINIMAL_MANIFEST.format(plugin_id="user-tool", name="User Tool"),
            encoding="utf-8",
        )

        # Local plugins dir is different
        local_plugins = tmp_path / "local_project" / ".obscura" / "plugins"
        local_plugins.mkdir(parents=True)

        loader = PluginLoader(plugin_dir=local_plugins)
        result = loader.discover_user()
        assert len(result) == 1
        assert result[0].id == "user-tool"


class TestLoadAll:
    """Tests for the load_all() pipeline including user plugins."""

    def test_load_all_includes_user_plugins_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """load_all() result dict includes user_plugins key."""
        global_home = tmp_path / "global"
        (global_home / "plugins").mkdir(parents=True)
        monkeypatch.setenv("OBSCURA_HOME", str(global_home))

        class FakeRegistry:
            def add(self, provider: object) -> None:
                pass

        loader = PluginLoader(plugin_dir=tmp_path / "local_plugins")
        result = loader.load_all(FakeRegistry())
        assert "user_plugins" in result
