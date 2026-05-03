"""obscura.core.compiler.loader — Parse spec files into Pydantic models.

Phase 1 of the compile pipeline: raw file I/O → typed spec objects.
Supports TOML (preferred) and YAML (deprecated) formats.
"""

from __future__ import annotations

import logging
import tomllib
import warnings
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from obscura.core.compiler.errors import SpecLoadError
from obscura.core.compiler.specs import (
    SPEC_KIND_MAP,
    AgentInstanceSpec,
    AnySpec,
    PackSpec,
    PolicySpec,
    TemplateSpec,
    WorkspaceSpec,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_SPEC_PATTERNS = ("**/*.toml", "**/*.yml", "**/*.yaml")


def load_spec_file(path: Path) -> AnySpec:
    """Load a single spec file and return the appropriate typed model.

    Supports ``.toml`` (preferred) and ``.yaml``/``.yml`` (deprecated).
    Dispatches on the ``kind`` field to select the correct Pydantic model.

    Raises
    ------
    SpecLoadError
        If the file cannot be read, parsed, or validated.

    """
    if not path.is_file():
        msg = f"Spec file not found: {path}"
        raise SpecLoadError(msg, source=str(path))

    suffix = path.suffix.lower()

    if suffix == ".toml":
        try:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        except Exception as exc:
            msg = f"Invalid TOML in {path}: {exc}"
            raise SpecLoadError(msg, source=str(path)) from exc
    elif suffix in (".yaml", ".yml"):
        warnings.warn(
            f"YAML spec files are deprecated; migrate {path.name} to TOML.",
            DeprecationWarning,
            stacklevel=2,
        )
        try:
            import yaml  # type: ignore[import-untyped]

            text = path.read_text(encoding="utf-8")
            raw = yaml.safe_load(text)
        except ImportError as exc:
            msg = f"PyYAML required to read {path}; install it or convert to TOML."
            raise SpecLoadError(
                msg,
                source=str(path),
            ) from exc
        except Exception as exc:
            msg = f"Invalid YAML in {path}: {exc}"
            raise SpecLoadError(msg, source=str(path)) from exc
    else:
        msg = f"Unsupported spec file extension: {suffix}"
        raise SpecLoadError(msg, source=str(path))

    if not isinstance(raw, dict):
        msg = f"Expected a mapping in {path}, got {type(raw).__name__}"
        raise SpecLoadError(
            msg,
            source=str(path),
        )
    raw_dict = cast(dict[str, Any], raw)

    kind_val = raw_dict.get("kind")
    kind: str | None = kind_val if isinstance(kind_val, str) else None
    if kind is None:
        msg = f"Missing 'kind' field in {path}"
        raise SpecLoadError(msg, source=str(path))

    model_cls: type[AnySpec] | None = SPEC_KIND_MAP.get(kind)
    if model_cls is None:
        msg = (
            f"Unknown kind {kind!r} in {path}. Expected one of: {sorted(SPEC_KIND_MAP)}"
        )
        raise SpecLoadError(
            msg,
            source=str(path),
        )

    try:
        return model_cls.model_validate(raw_dict)
    except ValidationError as exc:
        msg = f"Validation error in {path}: {exc}"
        raise SpecLoadError(
            msg,
            source=str(path),
        ) from exc


def load_specs_dir(directory: Path) -> SpecRegistry:
    """Load all spec files from a directory tree.

    Discovers ``*.toml`` (preferred), ``*.yml``, and ``*.yaml`` files.
    Returns a :class:`SpecRegistry` grouping specs by kind.
    """
    registry = SpecRegistry()

    if not directory.is_dir():
        logger.warning("Specs directory does not exist: %s", directory)
        return registry

    for pattern in _SPEC_PATTERNS:
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
    earlier ones (global), matching the ``config.toml`` merge precedent.
    """
    registry = SpecRegistry()
    for directory in directories:
        if not directory.is_dir():
            continue
        for pattern in _SPEC_PATTERNS:
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
        self.packs: dict[str, PackSpec] = {}
        self.workspaces: dict[str, WorkspaceSpec] = {}

    def add(self, spec: AnySpec) -> None:
        name: str = spec.metadata.name
        if isinstance(spec, TemplateSpec):
            self.templates[name] = spec
        elif isinstance(spec, AgentInstanceSpec):
            self.agents[name] = spec
        elif isinstance(spec, PolicySpec):
            self.policies[name] = spec
        elif isinstance(spec, PackSpec):
            self.packs[name] = spec
        else:
            self.workspaces[name] = spec

    def get_template(self, name: str) -> TemplateSpec | None:
        return self.templates.get(name)

    def get_policy(self, name: str) -> PolicySpec | None:
        return self.policies.get(name)

    def get_pack(self, name: str) -> PackSpec | None:
        return self.packs.get(name)

    def get_workspace(self, name: str) -> WorkspaceSpec | None:
        return self.workspaces.get(name)
