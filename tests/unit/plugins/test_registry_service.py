"""Comprehensive tests for obscura.plugins.registry (PluginRegistryService)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from obscura.plugins.models import PluginSpec, PluginStatus
from obscura.plugins.registry import PluginEntry, PluginRegistryService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(
    plugin_id: str = "test-plugin",
    name: str = "Test Plugin",
    version: str = "1.0.0",
    *,
    source_type: str = "local",
    runtime_type: str = "native",
    trust_level: str = "community",
    author: str = "tester",
    description: str = "A test plugin",
    capabilities: tuple = (),
    tools: tuple = (),
    workflows: tuple = (),
) -> PluginSpec:
    """Build a minimal PluginSpec for testing."""
    from obscura.plugins.models import CapabilitySpec, ToolContribution, WorkflowSpec

    caps = capabilities or (
        CapabilitySpec(id="test.read", version="1.0.0", description="read stuff"),
        CapabilitySpec(id="test.write", version="1.0.0", description="write stuff"),
    )
    tool_list = tools or (
        ToolContribution(name="test_tool", description="a tool"),
    )
    wf_list = workflows or (
        WorkflowSpec(id="test-wf", version="1.0.0", name="Test Workflow", description="a wf", steps=()),
    )
    return PluginSpec(
        id=plugin_id,
        name=name,
        version=version,
        source_type=source_type,
        runtime_type=runtime_type,
        trust_level=trust_level,
        author=author,
        description=description,
        capabilities=caps,
        tools=tool_list,
        workflows=wf_list,
    )


@pytest.fixture()
def spec() -> PluginSpec:
    return _make_spec()


@pytest.fixture()
def svc(tmp_path: Path) -> PluginRegistryService:
    return PluginRegistryService(plugin_dir=tmp_path)


# ===========================================================================
# PluginEntry dataclass
# ===========================================================================


class TestPluginEntryToDict:
    """to_dict round-trips correctly."""

    def test_roundtrip_preserves_all_fields(self, spec: PluginSpec) -> None:
        entry = PluginEntry.from_spec(spec, source="local")
        d = entry.to_dict()
        restored = PluginEntry.from_dict(d)

        assert restored.id == entry.id
        assert restored.name == entry.name
        assert restored.version == entry.version
        assert restored.source_type == entry.source_type
        assert restored.runtime_type == entry.runtime_type
        assert restored.trust_level == entry.trust_level
        assert restored.author == entry.author
        assert restored.description == entry.description
        assert restored.source == entry.source
        assert restored.enabled == entry.enabled
        assert restored.state == entry.state
        assert restored.error == entry.error
        assert restored.installed_at == entry.installed_at
        assert restored.updated_at == entry.updated_at
        assert restored.contributed_capabilities == entry.contributed_capabilities
        assert restored.contributed_tools == entry.contributed_tools
        assert restored.contributed_workflows == entry.contributed_workflows

    def test_to_dict_returns_dict_type(self) -> None:
        entry = PluginEntry(
            id="a", name="A", version="1.0.0", source_type="local",
            runtime_type="native", trust_level="community", author="", description="", source="",
        )
        assert isinstance(entry.to_dict(), dict)


class TestPluginEntryFromDict:
    """from_dict filters unknown keys and accepts all known keys."""

    def test_filters_unknown_keys(self) -> None:
        data = {
            "id": "x", "name": "X", "version": "0.1.0",
            "source_type": "local", "runtime_type": "native",
            "trust_level": "community", "author": "", "description": "",
            "source": "", "unknown_field": 42, "extra_junk": "hi",
        }
        entry = PluginEntry.from_dict(data)
        assert entry.id == "x"
        assert "unknown_field" not in entry.to_dict()
        assert "extra_junk" not in entry.to_dict()

    def test_accepts_all_known_keys(self) -> None:
        data = {
            "id": "full", "name": "Full", "version": "2.0.0",
            "source_type": "git", "runtime_type": "cli",
            "trust_level": "verified", "author": "alice",
            "description": "full entry", "source": "git+https://x",
            "enabled": True, "state": "enabled", "error": "some error",
            "installed_at": "2024-01-01T00:00:00", "updated_at": "2024-06-01T00:00:00",
            "contributed_capabilities": ["cap.one"],
            "contributed_tools": ["my_tool"],
            "contributed_workflows": ["wf-1"],
        }
        entry = PluginEntry.from_dict(data)
        assert entry.id == "full"
        assert entry.enabled is True
        assert entry.state == "enabled"
        assert entry.error == "some error"
        assert entry.installed_at == "2024-01-01T00:00:00"
        assert entry.contributed_capabilities == ["cap.one"]
        assert entry.contributed_tools == ["my_tool"]
        assert entry.contributed_workflows == ["wf-1"]

    def test_defaults_applied_for_missing_optional_keys(self) -> None:
        data = {
            "id": "min", "name": "Min", "version": "1.0.0",
            "source_type": "local", "runtime_type": "native",
            "trust_level": "community", "author": "", "description": "",
            "source": "",
        }
        entry = PluginEntry.from_dict(data)
        assert entry.enabled is False
        assert entry.state == "installed"
        assert entry.error is None
        assert entry.contributed_capabilities == []


class TestPluginEntryFromSpec:
    """from_spec creates entry with timestamps and contributed resources."""

    def test_creates_entry_with_all_contributed_resources(self, spec: PluginSpec) -> None:
        entry = PluginEntry.from_spec(spec, source="/some/path")
        assert entry.id == spec.id
        assert entry.name == spec.name
        assert entry.version == spec.version
        assert entry.source == "/some/path"
        assert entry.contributed_capabilities == ["test.read", "test.write"]
        assert entry.contributed_tools == ["test_tool"]
        assert entry.contributed_workflows == ["test-wf"]
        assert entry.enabled is False
        assert entry.state == "installed"

    def test_timestamps_are_set(self, spec: PluginSpec) -> None:
        entry = PluginEntry.from_spec(spec)
        assert entry.installed_at != ""
        assert entry.updated_at != ""
        assert entry.installed_at == entry.updated_at

    def test_empty_contributions_when_spec_has_none(self) -> None:
        bare = PluginSpec(
            id="bare-plugin", name="Bare", version="1.0.0",
            source_type="local", runtime_type="native",
        )
        entry = PluginEntry.from_spec(bare)
        assert entry.contributed_capabilities == []
        assert entry.contributed_tools == []
        assert entry.contributed_workflows == []

    def test_default_source_is_empty_string(self, spec: PluginSpec) -> None:
        entry = PluginEntry.from_spec(spec)
        assert entry.source == ""


# ===========================================================================
# PluginRegistryService — constructor
# ===========================================================================


class TestServiceInit:
    def test_creates_plugin_dir_and_registry_file(self, tmp_path: Path) -> None:
        d = tmp_path / "fresh"
        assert not d.exists()
        svc = PluginRegistryService(plugin_dir=d)
        assert d.exists()
        reg = d / "registry.json"
        assert reg.exists()
        assert json.loads(reg.read_text()) == []

    def test_does_not_overwrite_existing_registry(self, tmp_path: Path) -> None:
        reg = tmp_path / "registry.json"
        reg.write_text(json.dumps([{"id": "existing", "name": "E", "version": "1.0.0",
                                     "source_type": "local", "runtime_type": "native",
                                     "trust_level": "community", "author": "", "description": "",
                                     "source": ""}]))
        svc = PluginRegistryService(plugin_dir=tmp_path)
        plugins = svc.list_plugins()
        assert len(plugins) == 1
        assert plugins[0].id == "existing"

    def test_plugin_dir_property(self, tmp_path: Path) -> None:
        svc = PluginRegistryService(plugin_dir=tmp_path)
        assert svc.plugin_dir == tmp_path


# ===========================================================================
# list_plugins
# ===========================================================================


class TestListPlugins:
    def test_empty_registry_returns_empty_list(self, svc: PluginRegistryService) -> None:
        assert svc.list_plugins() == []

    def test_returns_all_entries(self, svc: PluginRegistryService) -> None:
        svc.install(_make_spec("plugin-a", "A", "1.0.0"))
        svc.install(_make_spec("plugin-b", "B", "2.0.0"))
        ids = [p.id for p in svc.list_plugins()]
        assert ids == ["plugin-a", "plugin-b"]


# ===========================================================================
# install
# ===========================================================================


class TestInstall:
    def test_adds_entry_and_persists(self, svc: PluginRegistryService, spec: PluginSpec, tmp_path: Path) -> None:
        entry = svc.install(spec, source="/src")
        assert entry.id == spec.id

        raw = json.loads((tmp_path / "registry.json").read_text())
        assert len(raw) == 1
        assert raw[0]["id"] == spec.id
        assert raw[0]["source"] == "/src"

    def test_reinstall_updates_entry_preserves_installed_at(
        self, svc: PluginRegistryService, spec: PluginSpec,
    ) -> None:
        first = svc.install(spec)
        original_installed_at = first.installed_at
        time.sleep(0.01)

        updated_spec = _make_spec(version="2.0.0")
        second = svc.install(updated_spec)

        assert second.version == "2.0.0"
        assert second.installed_at == original_installed_at
        assert len(svc.list_plugins()) == 1

    def test_reinstall_updates_updated_at(self, svc: PluginRegistryService) -> None:
        first = svc.install(_make_spec())
        time.sleep(0.01)
        second = svc.install(_make_spec(version="2.0.0"))
        assert second.updated_at >= first.updated_at

    def test_auto_enable_sets_enabled_and_state(self, svc: PluginRegistryService, spec: PluginSpec) -> None:
        entry = svc.install(spec, auto_enable=True)
        assert entry.enabled is True
        assert entry.state == "enabled"

    def test_auto_enable_false_by_default(self, svc: PluginRegistryService, spec: PluginSpec) -> None:
        entry = svc.install(spec)
        assert entry.enabled is False
        assert entry.state == "installed"


# ===========================================================================
# get_plugin
# ===========================================================================


class TestGetPlugin:
    def test_returns_entry_by_id(self, svc: PluginRegistryService, spec: PluginSpec) -> None:
        svc.install(spec)
        entry = svc.get_plugin(spec.id)
        assert entry is not None
        assert entry.id == spec.id

    def test_returns_none_for_missing(self, svc: PluginRegistryService) -> None:
        assert svc.get_plugin("nonexistent") is None

    def test_finds_correct_among_multiple(self, svc: PluginRegistryService) -> None:
        svc.install(_make_spec("alpha", "Alpha"))
        svc.install(_make_spec("beta", "Beta"))
        svc.install(_make_spec("gamma", "Gamma"))
        entry = svc.get_plugin("beta")
        assert entry is not None
        assert entry.name == "Beta"


# ===========================================================================
# enable / disable
# ===========================================================================


class TestEnableDisable:
    def test_enable_sets_flag_and_state(self, svc: PluginRegistryService, spec: PluginSpec) -> None:
        svc.install(spec)
        assert svc.enable(spec.id) is True
        entry = svc.get_plugin(spec.id)
        assert entry is not None
        assert entry.enabled is True
        assert entry.state == "enabled"

    def test_disable_clears_flag_and_state(self, svc: PluginRegistryService, spec: PluginSpec) -> None:
        svc.install(spec, auto_enable=True)
        assert svc.disable(spec.id) is True
        entry = svc.get_plugin(spec.id)
        assert entry is not None
        assert entry.enabled is False
        assert entry.state == "disabled"

    def test_enable_updates_updated_at(self, svc: PluginRegistryService, spec: PluginSpec) -> None:
        entry = svc.install(spec)
        old_updated = entry.updated_at
        time.sleep(0.01)
        svc.enable(spec.id)
        new_entry = svc.get_plugin(spec.id)
        assert new_entry is not None
        assert new_entry.updated_at >= old_updated

    def test_disable_updates_updated_at(self, svc: PluginRegistryService, spec: PluginSpec) -> None:
        entry = svc.install(spec, auto_enable=True)
        old_updated = entry.updated_at
        time.sleep(0.01)
        svc.disable(spec.id)
        new_entry = svc.get_plugin(spec.id)
        assert new_entry is not None
        assert new_entry.updated_at >= old_updated

    def test_enable_nonexistent_returns_false(self, svc: PluginRegistryService) -> None:
        assert svc.enable("nope") is False

    def test_disable_nonexistent_returns_false(self, svc: PluginRegistryService) -> None:
        assert svc.disable("nope") is False


# ===========================================================================
# uninstall
# ===========================================================================


class TestUninstall:
    def test_removes_from_registry(self, svc: PluginRegistryService, spec: PluginSpec) -> None:
        svc.install(spec)
        assert svc.uninstall(spec.id) is True
        assert svc.get_plugin(spec.id) is None
        assert svc.list_plugins() == []

    def test_returns_false_for_missing(self, svc: PluginRegistryService) -> None:
        assert svc.uninstall("does-not-exist") is False

    def test_deletes_local_directory(self, svc: PluginRegistryService, spec: PluginSpec, tmp_path: Path) -> None:
        svc.install(spec)
        local = tmp_path / spec.id
        local.mkdir()
        (local / "dummy.txt").write_text("hi")

        assert svc.uninstall(spec.id) is True
        assert not local.exists()

    def test_pip_uninstall_called_for_pip_source(self, svc: PluginRegistryService, tmp_path: Path) -> None:
        spec = _make_spec("pip-pkg", source_type="pip")
        svc.install(spec, source="some-pip-package")
        # The spec's source_type is already "pip"; verify it was stored correctly
        stored = svc.get_plugin("pip-pkg")
        assert stored is not None
        assert stored.source_type == "pip"

        with patch("obscura.plugins.registry.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert svc.uninstall("pip-pkg") is True
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            assert "uninstall" in call_args

    def test_preserves_other_plugins(self, svc: PluginRegistryService) -> None:
        svc.install(_make_spec("keep-me", "Keep"))
        svc.install(_make_spec("remove-me", "Remove"))
        svc.uninstall("remove-me")
        plugins = svc.list_plugins()
        assert len(plugins) == 1
        assert plugins[0].id == "keep-me"


# ===========================================================================
# list_enabled
# ===========================================================================


class TestListEnabled:
    def test_returns_only_enabled(self, svc: PluginRegistryService) -> None:
        svc.install(_make_spec("plugin-a"), auto_enable=True)
        svc.install(_make_spec("plugin-b"))
        svc.install(_make_spec("plugin-c"), auto_enable=True)
        enabled = svc.list_enabled()
        ids = sorted(p.id for p in enabled)
        assert ids == ["plugin-a", "plugin-c"]

    def test_empty_when_none_enabled(self, svc: PluginRegistryService) -> None:
        svc.install(_make_spec("plugin-a"))
        svc.install(_make_spec("plugin-b"))
        assert svc.list_enabled() == []


# ===========================================================================
# get_status
# ===========================================================================


class TestGetStatus:
    def test_returns_plugin_status(self, svc: PluginRegistryService, spec: PluginSpec) -> None:
        svc.install(spec)
        status = svc.get_status(spec.id)
        assert status is not None
        assert isinstance(status, PluginStatus)
        assert status.plugin_id == spec.id
        assert status.state == "installed"
        assert status.enabled is False

    def test_returns_none_for_missing(self, svc: PluginRegistryService) -> None:
        assert svc.get_status("nope") is None

    def test_reflects_enabled_state(self, svc: PluginRegistryService, spec: PluginSpec) -> None:
        svc.install(spec, auto_enable=True)
        status = svc.get_status(spec.id)
        assert status is not None
        assert status.enabled is True
        assert status.state == "enabled"

    def test_status_has_timestamps(self, svc: PluginRegistryService, spec: PluginSpec) -> None:
        svc.install(spec)
        status = svc.get_status(spec.id)
        assert status is not None
        assert status.installed_at != ""
        assert status.updated_at != ""


# ===========================================================================
# get_contributions
# ===========================================================================


class TestGetContributions:
    def test_returns_contributed_resources(self, svc: PluginRegistryService, spec: PluginSpec) -> None:
        svc.install(spec)
        contrib = svc.get_contributions(spec.id)
        assert contrib == {
            "capabilities": ["test.read", "test.write"],
            "tools": ["test_tool"],
            "workflows": ["test-wf"],
        }

    def test_returns_empty_dict_for_missing(self, svc: PluginRegistryService) -> None:
        assert svc.get_contributions("nope") == {}

    def test_empty_contributions(self, svc: PluginRegistryService) -> None:
        bare = PluginSpec(
            id="bare-plugin", name="Bare", version="1.0.0",
            source_type="local", runtime_type="native",
        )
        svc.install(bare)
        contrib = svc.get_contributions("bare-plugin")
        assert contrib == {"capabilities": [], "tools": [], "workflows": []}


# ===========================================================================
# Persistence across instances
# ===========================================================================


class TestPersistence:
    def test_data_survives_new_service_instance(self, tmp_path: Path, spec: PluginSpec) -> None:
        svc1 = PluginRegistryService(plugin_dir=tmp_path)
        svc1.install(spec, auto_enable=True)

        svc2 = PluginRegistryService(plugin_dir=tmp_path)
        plugins = svc2.list_plugins()
        assert len(plugins) == 1
        assert plugins[0].id == spec.id
        assert plugins[0].enabled is True
        assert plugins[0].contributed_capabilities == ["test.read", "test.write"]

    def test_enable_persists_across_instances(self, tmp_path: Path, spec: PluginSpec) -> None:
        svc1 = PluginRegistryService(plugin_dir=tmp_path)
        svc1.install(spec)
        svc1.enable(spec.id)

        svc2 = PluginRegistryService(plugin_dir=tmp_path)
        entry = svc2.get_plugin(spec.id)
        assert entry is not None
        assert entry.enabled is True


# ===========================================================================
# Corrupt / empty registry.json
# ===========================================================================


class TestCorruptRegistry:
    def test_corrupt_json_returns_empty(self, tmp_path: Path) -> None:
        reg = tmp_path / "registry.json"
        reg.write_text("{{{not valid json!!!")
        svc = PluginRegistryService(plugin_dir=tmp_path)
        assert svc.list_plugins() == []

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        reg = tmp_path / "registry.json"
        reg.write_text("")
        svc = PluginRegistryService(plugin_dir=tmp_path)
        assert svc.list_plugins() == []

    def test_json_object_instead_of_array_returns_empty(self, tmp_path: Path) -> None:
        reg = tmp_path / "registry.json"
        reg.write_text('{"not": "a list"}')
        svc = PluginRegistryService(plugin_dir=tmp_path)
        assert svc.list_plugins() == []


# ===========================================================================
# install_from_source — empty source
# ===========================================================================


class TestInstallFromSourceEmpty:
    def test_empty_string_returns_not_ok(self, svc: PluginRegistryService) -> None:
        result = svc.install_from_source("")
        assert result["ok"] is False
        assert "No source" in result["message"]

    def test_whitespace_only_returns_not_ok(self, svc: PluginRegistryService) -> None:
        result = svc.install_from_source("   ")
        assert result["ok"] is False


# ===========================================================================
# install_from_source — local path with manifest
# ===========================================================================


class TestInstallFromSourceLocalManifest:
    def test_installs_from_local_dir_with_manifest(self, svc: PluginRegistryService, tmp_path: Path) -> None:
        src_dir = tmp_path / "src_plugin"
        src_dir.mkdir()
        (src_dir / "plugin.yaml").write_text("id: local-plugin\nname: Local\nversion: 0.5.0\n")
        (src_dir / "code.py").write_text("print('hello')")

        mock_spec = _make_spec("local-plugin", "Local Plugin", "0.5.0")
        with patch("obscura.plugins.manifest.parse_manifest_file", return_value=mock_spec):
            result = svc.install_from_source(str(src_dir))

        assert result["ok"] is True
        assert "local-plugin" in result["message"]
        dest = tmp_path / "local-plugin"
        assert dest.exists()
        assert (dest / "code.py").exists()

    def test_manifest_parse_failure_returns_error(self, svc: PluginRegistryService, tmp_path: Path) -> None:
        src_dir = tmp_path / "bad_plugin"
        src_dir.mkdir()
        (src_dir / "plugin.yaml").write_text("bad: yaml: content")

        with patch("obscura.plugins.manifest.parse_manifest_file", side_effect=ValueError("bad manifest")):
            result = svc.install_from_source(str(src_dir))

        assert result["ok"] is False
        assert "failed" in result["message"].lower()


# ===========================================================================
# install_from_source — local dir without manifest (legacy)
# ===========================================================================


class TestInstallFromSourceLegacyLocal:
    def test_copies_local_dir_without_manifest(self, svc: PluginRegistryService, tmp_path: Path) -> None:
        src_dir = tmp_path / "external" / "legacy_plugin"
        src_dir.mkdir(parents=True)
        (src_dir / "main.py").write_text("# legacy")

        result = svc.install_from_source(str(src_dir))
        assert result["ok"] is True
        assert "Copied" in result["message"]
        assert (tmp_path / "legacy_plugin").exists()

    def test_legacy_already_exists_returns_error(self, svc: PluginRegistryService, tmp_path: Path) -> None:
        src_dir = tmp_path / "external" / "myplugin"
        src_dir.mkdir(parents=True)
        (src_dir / "code.py").write_text("# v1")
        # First install copies to tmp_path/myplugin
        result1 = svc.install_from_source(str(src_dir))
        assert result1["ok"] is True
        # Second install — destination already exists
        result2 = svc.install_from_source(str(src_dir))
        assert result2["ok"] is False
        assert "Already exists" in result2["message"]


# ===========================================================================
# install_from_source — git URL
# ===========================================================================


class TestInstallFromSourceGit:
    def test_git_clone_success_with_manifest(self, svc: PluginRegistryService, tmp_path: Path) -> None:
        mock_spec = _make_spec("git-plugin", "Git Plugin", "1.0.0")

        def fake_clone(cmd, **kwargs):
            dest = Path(cmd[3])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "plugin.yaml").write_text("id: git-plugin")
            return MagicMock(returncode=0, stderr="")

        with patch("obscura.plugins.registry.subprocess.run", side_effect=fake_clone), \
             patch("obscura.plugins.manifest.parse_manifest_file", return_value=mock_spec):
            result = svc.install_from_source("https://github.com/user/git-plugin.git")

        assert result["ok"] is True
        assert "git" in result["message"].lower()
        assert result["entry"].id == "git-plugin"

    def test_git_clone_failure(self, svc: PluginRegistryService) -> None:
        mock_proc = MagicMock(returncode=1, stderr="fatal: repo not found")
        with patch("obscura.plugins.registry.subprocess.run", return_value=mock_proc):
            result = svc.install_from_source("https://github.com/user/nonexistent.git")
        assert result["ok"] is False
        assert "git clone failed" in result["message"]

    def test_git_url_with_git_plus_prefix(self, svc: PluginRegistryService, tmp_path: Path) -> None:
        def fake_clone(cmd, **kwargs):
            dest = Path(cmd[3])
            dest.mkdir(parents=True, exist_ok=True)
            return MagicMock(returncode=0, stderr="")

        with patch("obscura.plugins.registry.subprocess.run", side_effect=fake_clone):
            result = svc.install_from_source("git+https://github.com/user/my-plugin.git")

        assert result["ok"] is True

    def test_git_already_exists(self, svc: PluginRegistryService, tmp_path: Path) -> None:
        (tmp_path / "existing-repo").mkdir()
        result = svc.install_from_source("https://github.com/user/existing-repo.git")
        assert result["ok"] is False
        assert "Already exists" in result["message"]


# ===========================================================================
# install_from_source — pip package
# ===========================================================================


class TestInstallFromSourcePip:
    def test_pip_install_success(self, svc: PluginRegistryService) -> None:
        mock_proc = MagicMock(returncode=0, stdout="Successfully installed my-package")
        with patch("obscura.plugins.registry.subprocess.run", return_value=mock_proc):
            result = svc.install_from_source("my-package")

        assert result["ok"] is True
        assert "pip" in result["message"].lower()
        assert "entry" in result
        entry = result["entry"]
        assert entry.source_type == "pip"
        assert entry.source == "my-package"
        assert entry.enabled is True

    def test_pip_install_failure(self, svc: PluginRegistryService) -> None:
        mock_proc = MagicMock(returncode=1, stderr="ERROR: No matching distribution")
        with patch("obscura.plugins.registry.subprocess.run", return_value=mock_proc):
            result = svc.install_from_source("nonexistent-package-xyz")

        assert result["ok"] is False
        assert "pip install failed" in result["message"]

    def test_pip_entry_recorded_in_registry(self, svc: PluginRegistryService) -> None:
        mock_proc = MagicMock(returncode=0, stdout="OK")
        with patch("obscura.plugins.registry.subprocess.run", return_value=mock_proc):
            svc.install_from_source("my-pip-pkg")

        plugins = svc.list_plugins()
        assert len(plugins) == 1
        assert plugins[0].source_type == "pip"
        assert plugins[0].runtime_type == "native"
        assert plugins[0].trust_level == "community"
