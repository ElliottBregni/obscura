from __future__ import annotations

from pathlib import Path

import pytest

from vault_gen.sync.base import Change, SyncAdapter, SyncResult
from vault_gen.sync.registry import AdapterRegistry, get_adapter, list_adapters


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeAdapter(SyncAdapter):
    ADAPTER_NAME = "fake"

    @property
    def name(self) -> str:
        return self.ADAPTER_NAME

    async def push(self, repo, config) -> SyncResult:  # type: ignore[override]
        return SyncResult(success=True, adapter=self.name)

    async def pull(self, repo, config) -> SyncResult:  # type: ignore[override]
        return SyncResult(success=True, adapter=self.name)

    async def diff(self, repo, config) -> list[Change]:  # type: ignore[override]
        return []


# ---------------------------------------------------------------------------
# AdapterRegistry
# ---------------------------------------------------------------------------


class TestAdapterRegistry:
    def test_unleash_registered_by_default(self) -> None:
        registry = AdapterRegistry()
        assert "unleash" in registry.list_adapters()

    def test_list_adapters_is_sorted(self) -> None:
        registry = AdapterRegistry()
        names = registry.list_adapters()
        assert names == sorted(names)

    def test_get_adapter_returns_instance(self) -> None:
        registry = AdapterRegistry()
        adapter = registry.get_adapter("unleash")
        assert adapter.name == "unleash"

    def test_get_adapter_raises_on_unknown(self) -> None:
        registry = AdapterRegistry()
        with pytest.raises(KeyError, match="no-such-adapter"):
            registry.get_adapter("no-such-adapter")

    def test_error_message_lists_available(self) -> None:
        registry = AdapterRegistry()
        with pytest.raises(KeyError) as exc_info:
            registry.get_adapter("bogus")
        assert "unleash" in str(exc_info.value)

    def test_get_adapter_returns_fresh_instance_each_call(self) -> None:
        registry = AdapterRegistry()
        a = registry.get_adapter("unleash")
        b = registry.get_adapter("unleash")
        assert a is not b

    def test_manual_registration_via_internal_dict(self) -> None:
        """Simulate a third-party adapter being loaded."""
        registry = AdapterRegistry()
        registry._adapters["fake"] = _FakeAdapter
        assert "fake" in registry.list_adapters()
        adapter = registry.get_adapter("fake")
        assert adapter.name == "fake"


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------


class TestConvenienceFunctions:
    def test_list_adapters_includes_unleash(self) -> None:
        assert "unleash" in list_adapters()

    def test_get_adapter_returns_unleash(self) -> None:
        adapter = get_adapter("unleash")
        assert adapter.name == "unleash"

    def test_get_adapter_raises_on_unknown(self) -> None:
        with pytest.raises(KeyError):
            get_adapter("does-not-exist")


# ---------------------------------------------------------------------------
# SyncConfig loading (part of registry integration)
# ---------------------------------------------------------------------------


class TestSyncConfig:
    def test_loads_empty_when_no_sync_toml(self, tmp_path: Path) -> None:
        from vault_gen.sync.config import SyncConfig

        cfg = SyncConfig.load(tmp_path)
        assert cfg.adapters == []

    def test_loads_adapter_from_toml(self, tmp_path: Path) -> None:
        from vault_gen.sync.config import SyncConfig

        (tmp_path / "sync.toml").write_text(
            '[[adapters]]\nname = "unleash"\nenabled = true\n\n'
            '[adapters.config]\nbase_url = "http://unleash:4242"\n'
        )
        cfg = SyncConfig.load(tmp_path)
        assert len(cfg.adapters) == 1
        assert cfg.adapters[0].name == "unleash"
        assert cfg.adapters[0].enabled is True
        assert cfg.adapters[0].config["base_url"] == "http://unleash:4242"

    def test_disabled_adapter_excluded_from_enabled(self, tmp_path: Path) -> None:
        from vault_gen.sync.config import SyncConfig

        (tmp_path / "sync.toml").write_text(
            '[[adapters]]\nname = "unleash"\nenabled = false\n\n'
            '[adapters.config]\nbase_url = "http://unleash"\n'
        )
        cfg = SyncConfig.load(tmp_path)
        assert cfg.enabled_adapters() == []

    def test_get_adapter_config_by_name(self, tmp_path: Path) -> None:
        from vault_gen.sync.config import SyncConfig

        (tmp_path / "sync.toml").write_text(
            '[[adapters]]\nname = "unleash"\nenabled = true\n\n'
            '[adapters.config]\nbase_url = "http://unleash"\n'
        )
        cfg = SyncConfig.load(tmp_path)
        ac = cfg.get_adapter_config("unleash")
        assert ac is not None
        assert ac.name == "unleash"

    def test_get_adapter_config_returns_none_for_unknown(self, tmp_path: Path) -> None:
        from vault_gen.sync.config import SyncConfig

        cfg = SyncConfig.load(tmp_path)
        assert cfg.get_adapter_config("no-such-adapter") is None

    def test_multiple_adapters(self, tmp_path: Path) -> None:
        from vault_gen.sync.config import SyncConfig

        (tmp_path / "sync.toml").write_text(
            '[[adapters]]\nname = "unleash"\nenabled = true\n\n'
            '[adapters.config]\nbase_url = "http://unleash"\n\n'
            '[[adapters]]\nname = "datadog"\nenabled = false\n\n'
            '[adapters.config]\napi_key = "dd-key"\n'
        )
        cfg = SyncConfig.load(tmp_path)
        assert len(cfg.adapters) == 2
        assert len(cfg.enabled_adapters()) == 1


# ---------------------------------------------------------------------------
# SyncState
# ---------------------------------------------------------------------------


class TestSyncState:
    def test_loads_empty_when_no_state_file(self, tmp_path: Path) -> None:
        from vault_gen.sync.config import SyncState

        state = SyncState.load(tmp_path)
        assert state.adapters == {}

    def test_record_push_persists(self, tmp_path: Path) -> None:
        from vault_gen.sync.config import SyncState

        state = SyncState.load(tmp_path)
        state.record_push("unleash", 3)
        state.save(tmp_path)

        loaded = SyncState.load(tmp_path)
        assert loaded.adapters["unleash"].last_push_changes == 3
        assert loaded.adapters["unleash"].last_push is not None

    def test_record_pull_persists(self, tmp_path: Path) -> None:
        from vault_gen.sync.config import SyncState

        state = SyncState.load(tmp_path)
        state.record_pull("unleash", 1)
        state.save(tmp_path)

        loaded = SyncState.load(tmp_path)
        assert loaded.adapters["unleash"].last_pull_changes == 1
        assert loaded.adapters["unleash"].last_pull is not None

    def test_push_and_pull_independent(self, tmp_path: Path) -> None:
        from vault_gen.sync.config import SyncState

        state = SyncState.load(tmp_path)
        state.record_push("unleash", 2)
        state.record_pull("unleash", 5)
        state.save(tmp_path)

        loaded = SyncState.load(tmp_path)
        a = loaded.adapters["unleash"]
        assert a.last_push_changes == 2
        assert a.last_pull_changes == 5
        assert a.last_push is not None
        assert a.last_pull is not None
