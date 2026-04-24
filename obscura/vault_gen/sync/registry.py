from __future__ import annotations

from importlib.metadata import entry_points

import structlog

from obscura.vault_gen.sync.base import SyncAdapter

log = structlog.get_logger()


class AdapterRegistry:
    """Discovers and provides access to SyncAdapter implementations.

    Discovery order (later entries win on name collision):
    1. Built-in adapters (unleash)
    2. Third-party adapters registered under the ``vault_gen.sync_adapters``
       entry point group

    To register a third-party adapter, add to its pyproject.toml::

        [project.entry-points."vault_gen.sync_adapters"]
        my-adapter = "my_package.adapter:MyAdapter"
    """

    def __init__(self) -> None:
        self._adapters: dict[str, type[SyncAdapter]] = {}
        self._load_builtins()
        self._load_entry_points()

    def _load_builtins(self) -> None:
        from obscura.vault_gen.sync.adapters.unleash import UnleashAdapter

        self._adapters[UnleashAdapter.ADAPTER_NAME] = UnleashAdapter
        log.debug("registered built-in adapter", name=UnleashAdapter.ADAPTER_NAME)

    def _load_entry_points(self) -> None:
        try:
            eps = entry_points(group="vault_gen.sync_adapters")
        except Exception as exc:
            log.warning("failed to query entry points", error=str(exc))
            return

        for ep in eps:
            try:
                cls: type[SyncAdapter] = ep.load()
                instance = cls()
                self._adapters[instance.name] = cls
                log.info("loaded adapter via entry point", name=instance.name, ep=ep.name)
            except Exception as exc:
                log.warning("failed to load adapter entry point", ep=ep.name, error=str(exc))

    def get_adapter(self, name: str) -> SyncAdapter:
        cls = self._adapters.get(name)
        if cls is None:
            raise KeyError(
                f"No adapter '{name}' registered. Available: {self.list_adapters()}"
            )
        return cls()

    def list_adapters(self) -> list[str]:
        return sorted(self._adapters.keys())


def get_adapter(name: str) -> SyncAdapter:
    """Convenience wrapper — creates a fresh registry each call."""
    return AdapterRegistry().get_adapter(name)


def list_adapters() -> list[str]:
    """Convenience wrapper — creates a fresh registry each call."""
    return AdapterRegistry().list_adapters()
