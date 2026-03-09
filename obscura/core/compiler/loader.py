"""obscura.core.compiler.loader — Parse YAML spec files into Pydantic models.

Phase 1 of the compile pipeline: raw file I/O → typed spec objects.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from obscura.core.compiler.errors import SpecLoadError
from obscura.core.compiler.specs import (
    SPEC_KIND_MAP,
    AgentInstanceSpec,
    AnySpec,
    PolicySpec,
    TemplateSpec,
    WorkspaceSpec,
)

logger = logging.getLogger(__name__)


def load_spec_file(path: Path) -> AnySpec:
    """Load a single YAML spec file and return the appropriate typed model.

    Dispatches on the ``kind`` field to select the correct Pydantic model.

    Raises
    ------
    SpecLoadError
        If the file cannot be read, parsed, or validated.
    """
    if not path.is_file():
        raise SpecLoadError(f"Spec file not found: {path}", source=str(path))

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecLoadError(f"Cannot read {path}: {exc}", source=str(path)) from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecLoadError(f"Invalid YAML in {path}: {exc}", source=str(path)) from exc

    if not isinstance(raw, dict):
        raise SpecLoadError(
            f"Expected a YAML mapping in {path}, got {type(raw).__name__}",
            source=str(path),
        )

    kind: str | None = raw.get("kind")
    if kind is None:
        raise SpecLoadError(f"Missing 'kind' field in {path}", source=str(path))

    model_cls: type[AnySpec] | None = SPEC_KIND_MAP.get(kind)
    if model_cls is None:
        raise SpecLoadError(
            f"Unknown kind {kind!r} in {path}. "
            f"Expected one of: {sorted(SPEC_KIND_MAP)}",
            source=str(path),
        )

    try:
        return model_cls.model_validate(raw)
    except ValidationError as exc:
        raise SpecLoadError(
            f"Validation error in {path}: {exc}",
            source=str(path),
        ) from exc


def load_specs_dir(directory: Path) -> SpecRegistry:
    """Load all ``*.yml`` and ``*.yaml`` spec files from a directory tree.

    Returns a :class:`SpecRegistry` grouping specs by kind.
    """
    registry = SpecRegistry()

    if not directory.is_dir():
        logger.warning("Specs directory does not exist: %s", directory)
        return registry

    for pattern in ("**/*.yml", "**/*.yaml"):
        for path in sorted(directory.glob(pattern)):
            if not path.is_file():
                continue
            try:
                spec = load_spec_file(path)
            except SpecLoadError:
                logger.warning("Failed to load spec %s", path, exc_info=True)
                continue
            registry.add(spec)

    return registry


def load_specs_dirs(directories: list[Path]) -> SpecRegistry:
    """Load specs from multiple directories, merging into a single registry.

    Directories are processed in order: global first, then local.
    When specs share the same kind + name, later entries (local) override
    earlier ones (global), matching the ``config.yaml`` merge precedent.
    """
    registry = SpecRegistry()
    for directory in directories:
        if not directory.is_dir():
            continue
        for pattern in ("**/*.yml", "**/*.yaml"):
            for path in sorted(directory.glob(pattern)):
                if not path.is_file():
                    continue
                try:
                    spec = load_spec_file(path)
                except SpecLoadError:
                    logger.warning("Failed to load spec %s", path, exc_info=True)
                    continue
                registry.add(spec)
    return registry


class SpecRegistry:
    """In-memory index of loaded specs, grouped by kind."""

    def __init__(self) -> None:
        self.templates: dict[str, TemplateSpec] = {}
        self.agents: dict[str, AgentInstanceSpec] = {}
        self.policies: dict[str, PolicySpec] = {}
        self.workspaces: dict[str, WorkspaceSpec] = {}

    def add(self, spec: AnySpec) -> None:
        name: str = spec.metadata.name
        if isinstance(spec, TemplateSpec):
            self.templates[name] = spec
        elif isinstance(spec, AgentInstanceSpec):
            self.agents[name] = spec
        elif isinstance(spec, PolicySpec):
            self.policies[name] = spec
        else:
            self.workspaces[name] = spec

    def get_template(self, name: str) -> TemplateSpec | None:
        return self.templates.get(name)

    def get_policy(self, name: str) -> PolicySpec | None:
        return self.policies.get(name)

    def get_workspace(self, name: str) -> WorkspaceSpec | None:
        return self.workspaces.get(name)
