from __future__ import annotations

import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import structlog
from pydantic import BaseModel

log = structlog.get_logger()

_STATE_FILE = ".vault-gen/sync-state.json"


class AdapterConfig(BaseModel, extra="forbid"):
    name: str
    enabled: bool = True
    config: dict[str, object] = {}


class SyncConfig(BaseModel, extra="forbid"):
    adapters: list[AdapterConfig] = []

    @classmethod
    def load(cls, repo_root: Path) -> SyncConfig:
        sync_file = repo_root / "sync.toml"
        if not sync_file.exists():
            log.debug("no sync.toml found", path=str(sync_file))
            return cls()
        data = tomllib.loads(sync_file.read_text())
        return cls.model_validate(data)

    def get_adapter_config(self, adapter_name: str) -> AdapterConfig | None:
        return next((a for a in self.adapters if a.name == adapter_name), None)

    def enabled_adapters(self) -> list[AdapterConfig]:
        return [a for a in self.adapters if a.enabled]


class AdapterState(BaseModel, extra="ignore"):
    """Per-adapter sync state persisted in .vault-gen/sync-state.json."""

    last_push: str | None = None
    last_pull: str | None = None
    last_push_changes: int = 0
    last_pull_changes: int = 0


class SyncState(BaseModel, extra="ignore"):
    adapters: dict[str, AdapterState] = {}

    @classmethod
    def load(cls, repo_root: Path) -> SyncState:
        state_file = repo_root / _STATE_FILE
        if not state_file.exists():
            return cls()
        try:
            return cls.model_validate(json.loads(state_file.read_text()))
        except Exception as exc:
            log.warning("could not load sync state", error=str(exc))
            return cls()

    def record_push(self, adapter_name: str, n_changes: int) -> None:
        state = self.adapters.setdefault(adapter_name, AdapterState())
        state = AdapterState(
            last_push=datetime.now(UTC).isoformat(),
            last_push_changes=n_changes,
            last_pull=state.last_pull,
            last_pull_changes=state.last_pull_changes,
        )
        self.adapters[adapter_name] = state

    def record_pull(self, adapter_name: str, n_changes: int) -> None:
        state = self.adapters.setdefault(adapter_name, AdapterState())
        state = AdapterState(
            last_pull=datetime.now(UTC).isoformat(),
            last_pull_changes=n_changes,
            last_push=state.last_push,
            last_push_changes=state.last_push_changes,
        )
        self.adapters[adapter_name] = state

    def save(self, repo_root: Path) -> None:
        state_file = repo_root / _STATE_FILE
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(self.model_dump(), indent=2) + "\n")
