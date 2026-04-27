from __future__ import annotations

import json
from pathlib import Path

import structlog
from pydantic import BaseModel

log = structlog.get_logger()

_REGISTRY_DIR = Path.home() / ".vault-gen"
_REGISTRY_FILE = _REGISTRY_DIR / "registry.json"


class RegistryEntry(BaseModel, extra="forbid"):
    name: str
    path: str
    repo_type: str | None
    obscura_path: str | None = None


class Registry(BaseModel, extra="forbid"):
    entries: dict[str, RegistryEntry] = {}

    @classmethod
    def load(cls) -> Registry:
        if not _REGISTRY_FILE.exists():
            return cls()
        try:
            data = json.loads(_REGISTRY_FILE.read_text())
            return cls.model_validate(data)
        except Exception as exc:
            log.warning("failed to load registry, starting fresh", error=str(exc))
            return cls()

    def save(self) -> None:
        _REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
        _REGISTRY_FILE.write_text(json.dumps(self.model_dump(), indent=2) + "\n")
        log.debug("registry saved", path=str(_REGISTRY_FILE))

    def add(self, *, name: str, path: Path, repo_type: str | None) -> RegistryEntry:
        entry = RegistryEntry(name=name, path=str(path), repo_type=repo_type)
        self.entries[name] = entry
        return entry

    def find_by_path(self, path: Path) -> RegistryEntry | None:
        target = str(path)
        return next((e for e in self.entries.values() if e.path == target), None)
