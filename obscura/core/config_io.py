"""obscura.core.config_io — Format-agnostic config file I/O.

Provides helpers to load config files in TOML (preferred) or YAML (deprecated),
and to write TOML files.  All other modules should use these helpers instead of
importing ``tomllib`` or ``yaml`` directly for config loading.
"""

from __future__ import annotations

import copy
import logging
import tomllib
import warnings
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def load_config(path: Path, *, warn_yaml: bool = True) -> dict[str, Any]:
    """Load a config file, auto-detecting format from extension.

    Supported extensions: ``.toml``, ``.yaml``, ``.yml``.
    YAML files emit a :class:`DeprecationWarning` unless *warn_yaml*
    is ``False`` (useful for files that are intentionally YAML, such as
    ``agents.yaml``).

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    ValueError
        If the file extension is unsupported or parsing fails.

    """
    if not path.is_file():
        raise FileNotFoundError(path)

    suffix = path.suffix.lower()

    if suffix == ".toml":
        return _load_toml(path)

    if suffix in (".yaml", ".yml"):
        if warn_yaml:
            warnings.warn(
                f"YAML config files are deprecated; migrate {path.name} to TOML.",
                DeprecationWarning,
                stacklevel=2,
            )
        return _load_yaml(path)

    msg = f"Unsupported config file extension: {suffix}"
    raise ValueError(msg)


def try_load_config(
    *candidates: Path,
    warn_yaml: bool = True,
) -> dict[str, Any] | None:
    """Try loading the first existing file from *candidates*.

    Returns ``None`` if no candidate exists.  Useful for the common
    pattern of trying ``config.toml`` then ``config.yaml``.
    """
    for path in candidates:
        if path.is_file():
            return load_config(path, warn_yaml=warn_yaml)
    return None


def dump_toml(data: dict[str, Any], path: Path) -> None:
    """Write *data* as TOML to *path*."""
    import tomli_w  # type: ignore[import-untyped]

    with open(path, "wb") as f:
        tomli_w.dump(data, f)


def dumps_toml(data: dict[str, Any]) -> str:
    """Serialize *data* to a TOML string."""
    import tomli_w  # type: ignore[import-untyped]

    return tomli_w.dumps(data)


def apply_agent_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge a top-level ``defaults`` dict into every agent entry.

    If *raw* contains a ``"defaults"`` key (must be a dict), each agent
    under ``raw["agents"]`` receives a fresh copy of the defaults merged
    with its own fields (agent values win).  The ``"defaults"`` key is
    stripped from the returned dict.

    Supports both list-of-dicts (YAML-style) and dict-of-dicts (TOML-style)
    agent collections.

    The input dict is **not** mutated; a new dict is always returned when
    defaults are present.  If no ``"defaults"`` key is present the original
    dict is returned unchanged.
    """
    if "defaults" not in raw:
        return raw

    raw_defaults = raw["defaults"]
    if not isinstance(raw_defaults, dict):
        return raw
    defaults = cast(dict[str, Any], raw_defaults)

    result: dict[str, Any] = {k: v for k, v in raw.items() if k != "defaults"}
    agents: Any = result.get("agents")

    if isinstance(agents, list):
        merged_list: list[Any] = []
        for agent_cfg in cast(list[Any], agents):
            if isinstance(agent_cfg, dict):
                merged_list.append(
                    _deep_merge_new(defaults, cast(dict[str, Any], agent_cfg))
                )
            else:
                merged_list.append(agent_cfg)
        result["agents"] = merged_list
    elif isinstance(agents, dict):
        merged_dict: dict[str, Any] = {}
        for name, agent_cfg in cast(dict[str, Any], agents).items():
            if isinstance(agent_cfg, dict):
                merged_dict[name] = _deep_merge_new(
                    defaults, cast(dict[str, Any], agent_cfg)
                )
            else:
                merged_dict[name] = agent_cfg
        result["agents"] = merged_dict

    return result


def load_merged_agents(
    home: Path,
    *,
    include_disabled: bool = False,
) -> dict[str, dict[str, Any]]:
    """Load and merge agents from primary and catalog files.

    Reads ``agents.yaml`` (or ``.toml`` fallback) as the primary source
    and ``agents-available.yaml`` (or ``.toml`` fallback) as the catalog.
    Applies top-level ``defaults`` to each agent entry.  Primary agents
    override catalog agents when names collide.

    For catalog agents, ``enabled`` defaults to ``False``.  For primary
    agents, ``enabled`` defaults to ``True``.

    Returns a mapping of agent name to config dict, filtering out
    disabled agents unless *include_disabled* is ``True``.
    """
    primary_raw = (
        try_load_config(
            home / "agents.yaml",
            home / "agents.toml",
            warn_yaml=False,
        )
        or {}
    )

    catalog_raw = (
        try_load_config(
            home / "agents-available.yaml",
            home / "agents-available.toml",
            warn_yaml=False,
        )
        or {}
    )

    merged: dict[str, dict[str, Any]] = {}

    # Catalog agents: enabled defaults to False.
    for entry in _extract_agents_with_defaults(catalog_raw):
        name = entry.get("name", "")
        if not name:
            continue
        entry.setdefault("enabled", False)
        merged[name] = entry

    # Primary agents override catalog; enabled defaults to True.
    for entry in _extract_agents_with_defaults(primary_raw):
        name = entry.get("name", "")
        if not name:
            continue
        entry.setdefault("enabled", True)
        merged[name] = entry

    if not include_disabled:
        merged = {n: c for n, c in merged.items() if c.get("enabled", True)}

    return merged


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _extract_agents_with_defaults(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract agent entries from *raw*, applying top-level ``defaults``.

    Handles both list-format (``agents: [{name: ..., ...}, ...]``) and
    dict-format (``agents: {name: {...}, ...}``) agent configurations.
    Returns a list of deep-copied agent dicts with defaults merged in.
    """
    defaults_raw = raw.get("defaults", {})
    defaults: dict[str, Any] = (
        cast(dict[str, Any], defaults_raw) if isinstance(defaults_raw, dict) else {}
    )

    agents_val = raw.get("agents")
    if agents_val is None:
        return []

    entries: list[dict[str, Any]] = []

    if isinstance(agents_val, list):
        # List format: [{name: "assistant", ...}, ...]
        for agent in cast(list[Any], agents_val):
            if not isinstance(agent, dict):
                continue
            merged = _deep_merge_new(defaults, cast(dict[str, Any], agent))
            entries.append(merged)
    elif isinstance(agents_val, dict):
        # Dict format: {assistant: {...}, ...}
        for name, agent_cfg in cast(dict[str, Any], agents_val).items():
            if not isinstance(agent_cfg, dict):
                continue
            merged = _deep_merge_new(defaults, cast(dict[str, Any], agent_cfg))
            merged.setdefault("name", name)
            entries.append(merged)

    return entries


def _deep_merge_new(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge *base* and *override* into a **new** dict.

    Override values win.  Nested dicts are merged recursively; everything
    else (lists, scalars) is replaced entirely by the override value.
    Neither input is mutated.
    """
    result: dict[str, Any] = copy.deepcopy(base)
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = _deep_merge_new(
                cast(dict[str, Any], existing), cast(dict[str, Any], value)
            )
        else:
            result[key] = copy.deepcopy(value)
    return result


def _load_toml(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:
        msg = f"PyYAML is required to read {path.name}; install it or convert to TOML."
        raise ValueError(
            msg,
        ) from exc

    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        msg = f"Expected a mapping in {path}, got {type(raw).__name__}"
        raise ValueError(msg)
    return cast(dict[str, Any], raw)
