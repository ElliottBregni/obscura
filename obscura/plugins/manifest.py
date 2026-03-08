"""Plugin manifest parser and validator.

Reads ``plugin.yaml`` files and produces validated ``PluginSpec`` instances.
Uses safe YAML loading — no code execution at parse time.

Usage::

    from obscura.plugins.manifest import parse_manifest, parse_manifest_file

    spec = parse_manifest_file(Path("my-plugin/plugin.yaml"))
    spec = parse_manifest(yaml_dict)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from obscura.plugins.models import (
    CapabilitySpec,
    ConfigRequirement,
    HealthcheckSpec,
    InstructionSpec,
    PluginSpec,
    PolicyHintSpec,
    ToolContribution,
    WorkflowSpec,
    validate_capability_id,
    validate_plugin_id,
    validate_semver,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ManifestError(Exception):
    """Raised when a plugin manifest is invalid."""

    def __init__(self, message: str, path: Path | None = None) -> None:
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require(data: dict[str, Any], key: str, path: Path | None = None) -> Any:
    """Return ``data[key]`` or raise ``ManifestError``."""
    if key not in data:
        raise ManifestError(f"Missing required field: {key!r}", path)
    return data[key]


def _parse_config_requirements(
    raw: dict[str, Any] | list[dict[str, Any]] | None,
) -> tuple[ConfigRequirement, ...]:
    if raw is None:
        return ()
    # Support both dict-form (key → spec) and list-form
    items: list[ConfigRequirement] = []
    if isinstance(raw, dict):
        for key, spec in raw.items():
            if isinstance(spec, dict):
                items.append(ConfigRequirement(
                    key=key,
                    type=spec.get("type", "string"),
                    required=spec.get("required", True),
                    description=spec.get("description", ""),
                    default=spec.get("default"),
                ))
            else:
                items.append(ConfigRequirement(key=key))
    elif isinstance(raw, list):
        for entry in raw:
            items.append(ConfigRequirement(
                key=entry.get("key", entry.get("name", "")),
                type=entry.get("type", "string"),
                required=entry.get("required", True),
                description=entry.get("description", ""),
                default=entry.get("default"),
            ))
    return tuple(items)


def _parse_capabilities(
    raw: list[dict[str, Any]] | None,
) -> tuple[CapabilitySpec, ...]:
    if not raw:
        return ()
    items: list[CapabilitySpec] = []
    for entry in raw:
        tools_raw = entry.get("tools", [])
        if isinstance(tools_raw, str):
            tools_raw = [tools_raw]
        items.append(CapabilitySpec(
            id=entry["id"],
            version=entry.get("version", "1.0.0"),
            description=entry.get("description", ""),
            tools=tuple(tools_raw),
            requires_approval=entry.get("requires_approval", False),
            default_grant=entry.get("default_grant", True),
        ))
    return tuple(items)


def _parse_tools(
    raw: list[dict[str, Any]] | None,
) -> tuple[ToolContribution, ...]:
    if not raw:
        return ()
    items: list[ToolContribution] = []
    for entry in raw:
        items.append(ToolContribution(
            name=entry["name"],
            description=entry.get("description", ""),
            parameters=entry.get("parameters", {}),
            handler_ref=entry.get("handler", entry.get("handler_ref", "")),
            capability=entry.get("capability", ""),
            side_effects=entry.get("side_effects", "none"),
            required_tier=entry.get("required_tier", "public"),
            timeout_seconds=float(entry.get("timeout_seconds", 60.0)),
            retries=int(entry.get("retries", 0)),
        ))
    return tuple(items)


def _parse_workflows(
    raw: list[dict[str, Any]] | None,
) -> tuple[WorkflowSpec, ...]:
    if not raw:
        return ()
    items: list[WorkflowSpec] = []
    for entry in raw:
        caps = entry.get("required_capabilities", [])
        if isinstance(caps, str):
            caps = [caps]
        steps = entry.get("steps", [])
        items.append(WorkflowSpec(
            id=entry["id"],
            version=entry.get("version", "1.0.0"),
            name=entry.get("name", entry["id"]),
            description=entry.get("description", ""),
            steps=tuple(steps),
            required_capabilities=tuple(caps),
        ))
    return tuple(items)


def _parse_instructions(
    raw: list[dict[str, Any]] | None,
) -> tuple[InstructionSpec, ...]:
    if not raw:
        return ()
    items: list[InstructionSpec] = []
    for entry in raw:
        items.append(InstructionSpec(
            id=entry["id"],
            version=entry.get("version", "1.0.0"),
            scope=entry.get("scope", "agent"),
            content=entry.get("content", ""),
            priority=int(entry.get("priority", 50)),
        ))
    return tuple(items)


def _parse_policy_hints(
    raw: list[dict[str, Any]] | None,
) -> tuple[PolicyHintSpec, ...]:
    if not raw:
        return ()
    items: list[PolicyHintSpec] = []
    for entry in raw:
        items.append(PolicyHintSpec(
            capability_id=entry["capability_id"],
            recommended_action=entry.get("recommended_action", "allow"),
            reason=entry.get("reason", ""),
        ))
    return tuple(items)


def _parse_healthcheck(
    raw: dict[str, Any] | None,
) -> HealthcheckSpec | None:
    if not raw:
        return None
    return HealthcheckSpec(
        type=raw.get("type", "callable"),
        target=raw.get("target", ""),
        interval_seconds=int(raw.get("interval_seconds", 300)),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_manifest(
    data: dict[str, Any],
    *,
    source_path: Path | None = None,
) -> PluginSpec:
    """Parse a manifest dict into a validated ``PluginSpec``.

    Raises ``ManifestError`` on invalid or missing fields.
    """
    try:
        plugin_id = validate_plugin_id(_require(data, "id", source_path))
        version = validate_semver(_require(data, "version", source_path))
        name = data.get("name", plugin_id)

        return PluginSpec(
            id=plugin_id,
            name=name,
            version=version,
            source_type=data.get("source_type", "local"),
            runtime_type=data.get("runtime_type", "native"),
            trust_level=data.get("trust_level", "community"),
            author=data.get("author", ""),
            description=data.get("description", ""),
            config_requirements=_parse_config_requirements(data.get("config")),
            capabilities=_parse_capabilities(data.get("capabilities")),
            tools=_parse_tools(data.get("tools")),
            workflows=_parse_workflows(data.get("workflows")),
            instructions=_parse_instructions(data.get("instructions")),
            policy_hints=_parse_policy_hints(data.get("policy_hints")),
            install_hook=data.get("install_hook"),
            bootstrap_hook=data.get("bootstrap_hook"),
            healthcheck=_parse_healthcheck(data.get("healthcheck")),
        )
    except ManifestError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise ManifestError(str(exc), source_path) from exc


def parse_manifest_file(path: Path) -> PluginSpec:
    """Load and parse a ``plugin.yaml`` file.

    Raises ``ManifestError`` if the file is missing or invalid.
    """
    path = Path(path)
    if not path.exists():
        raise ManifestError(f"Manifest file not found: {path}", path)

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        # Fallback: try json if yaml not available
        import json
        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            raise ManifestError(f"Failed to parse manifest: {exc}", path) from exc
    else:
        try:
            data = yaml.safe_load(path.read_text())
        except Exception as exc:
            raise ManifestError(f"Failed to parse YAML: {exc}", path) from exc

    if not isinstance(data, dict):
        raise ManifestError("Manifest must be a YAML/JSON mapping", path)

    return parse_manifest(data, source_path=path)


__all__ = ["parse_manifest", "parse_manifest_file", "ManifestError"]
