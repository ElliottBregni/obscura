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
from typing import Any, cast, cast

from obscura.plugins.models import (
    BootstrapDep,
    BootstrapSpec,
    CapabilitySpec,
    ConfigRequirement,
    HealthcheckSpec,
    InstructionSpec,
    PluginSpec,
    PolicyHintSpec,
    ToolContribution,
    WorkflowSpec,
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
        msg = f"Missing required field: {key!r}"
        raise ManifestError(msg, path)
    return data[key]


def parse_config_requirements(
    raw: dict[str, Any] | list[dict[str, Any]] | None,
) -> tuple[ConfigRequirement, ...]:
    if raw is None:
        return ()
    # Support both dict-form (key → spec) and list-form
    items: list[ConfigRequirement] = []
    if isinstance(raw, dict):
        for key, spec in raw.items():
            if isinstance(spec, dict):
                spec_d: dict[str, Any] = cast("dict[str, Any]", spec)
                items.append(
                    ConfigRequirement(
                        key=key,
                        type=spec_d.get("type", "string"),
                        required=spec_d.get("required", True),
                        description=spec_d.get("description", ""),
                        default=spec_d.get("default"),
                    ),
                )
            else:
                items.append(ConfigRequirement(key=key))
    elif isinstance(raw, list):
        for _entry in raw:
            entry: dict[str, Any] = cast("dict[str, Any]", _entry)
            items.append(
                ConfigRequirement(
                    key=entry.get("key", entry.get("name", "")),
                    type=entry.get("type", "string"),
                    required=entry.get("required", True),
                    description=entry.get("description", ""),
                    default=entry.get("default"),
                ),
            )
    return tuple(items)


def parse_capabilities(
    raw: list[dict[str, Any]] | dict[str, Any] | None,
) -> tuple[CapabilitySpec, ...]:
    if not raw:
        return ()
    items: list[CapabilitySpec] = []
    # Support dict-form {id: {fields}} and list-form [{id: ..., fields}]
    entries: list[dict[str, Any]]
    if isinstance(raw, dict):
        entries = [
            {**spec, "id": cap_id} if isinstance(spec, dict) else {"id": cap_id}
            for cap_id, spec in raw.items()
        ]
    else:
        entries = raw
    for entry in entries:
        tools_raw = entry.get("tools", [])
        if isinstance(tools_raw, str):
            tools_raw = [tools_raw]
        items.append(
            CapabilitySpec(
                id=entry["id"],
                version=entry.get("version", "1.0.0"),
                description=entry.get("description", ""),
                tools=tuple(tools_raw),
                requires_approval=entry.get("requires_approval", False),
                default_grant=entry.get("default_grant", True),
            ),
        )
    return tuple(items)


def parse_tools(
    raw: list[dict[str, Any]] | dict[str, Any] | None,
) -> tuple[ToolContribution, ...]:
    if not raw:
        return ()
    items: list[ToolContribution] = []
    # Support dict-form {name: {fields}} and list-form [{name: ..., fields}]
    entries: list[dict[str, Any]]
    if isinstance(raw, dict):
        entries = [
            {**spec, "name": tool_name}
            if isinstance(spec, dict)
            else {"name": tool_name}
            for tool_name, spec in raw.items()
        ]
    else:
        entries = raw
    for entry in entries:
        items.append(
            ToolContribution(
                name=entry["name"],
                description=entry.get("description", ""),
                parameters=entry.get("parameters", {}),
                handler_ref=entry.get("handler", entry.get("handler_ref", "")),
                capability=entry.get("capability", ""),
                side_effects=entry.get("side_effects", "none"),
                required_tier=entry.get("required_tier", "public"),
                timeout_seconds=float(entry.get("timeout_seconds", 60.0)),
                retries=int(entry.get("retries", 0)),
            ),
        )
    return tuple(items)


def parse_workflows(
    raw: list[dict[str, Any]] | dict[str, Any] | None,
) -> tuple[WorkflowSpec, ...]:
    if not raw:
        return ()
    items: list[WorkflowSpec] = []
    # Support dict-form {id: {fields}} and list-form [{id: ..., fields}]
    entries: list[dict[str, Any]]
    if isinstance(raw, dict):
        entries = [
            {**spec, "id": wf_id} if isinstance(spec, dict) else {"id": wf_id}
            for wf_id, spec in raw.items()
        ]
    else:
        entries = raw
    for entry in entries:
        caps = entry.get("required_capabilities", [])
        if isinstance(caps, str):
            caps = [caps]
        steps = entry.get("steps", [])
        items.append(
            WorkflowSpec(
                id=entry["id"],
                version=entry.get("version", "1.0.0"),
                name=entry.get("name", entry["id"]),
                description=entry.get("description", ""),
                steps=tuple(steps),
                required_capabilities=tuple(caps),
            ),
        )
    return tuple(items)


def parse_instructions(
    raw: list[dict[str, Any]] | dict[str, Any] | None,
) -> tuple[InstructionSpec, ...]:
    if not raw:
        return ()
    items: list[InstructionSpec] = []
    # Support dict-form {id: {fields}} and list-form [{id: ..., fields}]
    entries: list[dict[str, Any]]
    if isinstance(raw, dict):
        entries = [
            {**spec, "id": instr_id} if isinstance(spec, dict) else {"id": instr_id}
            for instr_id, spec in raw.items()
        ]
    else:
        entries = raw
    for entry in entries:
        items.append(
            InstructionSpec(
                id=entry["id"],
                version=entry.get("version", "1.0.0"),
                scope=entry.get("scope", "agent"),
                content=entry.get("content", ""),
                priority=int(entry.get("priority", 50)),
            ),
        )
    return tuple(items)


def parse_policy_hints(
    raw: list[dict[str, Any]] | dict[str, Any] | None,
) -> tuple[PolicyHintSpec, ...]:
    if not raw:
        return ()
    items: list[PolicyHintSpec] = []
    # Support dict-form {capability_id: {fields}} and list-form [{capability_id: ..., fields}]
    entries: list[dict[str, Any]]
    if isinstance(raw, dict):
        entries = [
            {**spec, "capability_id": cap_id}
            if isinstance(spec, dict)
            else {"capability_id": cap_id}
            for cap_id, spec in raw.items()
        ]
    else:
        entries = raw
    for entry in entries:
        items.append(
            PolicyHintSpec(
                capability_id=entry["capability_id"],
                recommended_action=entry.get("recommended_action", "allow"),
                reason=entry.get("reason", ""),
            ),
        )
    return tuple(items)


def parse_healthcheck(
    raw: dict[str, Any] | None,
) -> HealthcheckSpec | None:
    if not raw:
        return None
    return HealthcheckSpec(
        type=raw.get("type", "callable"),
        target=raw.get("target", ""),
        interval_seconds=int(raw.get("interval_seconds", 300)),
    )


def parse_bootstrap(
    raw: dict[str, Any] | list[dict[str, Any]] | None,
) -> BootstrapSpec | None:
    if not raw:
        return None
    # Support shorthand list-of-deps or full spec
    if isinstance(raw, list):
        raw = {"deps": raw}
    deps_raw = raw.get("deps", [])
    deps: list[BootstrapDep] = []
    for entry in deps_raw:
        if isinstance(entry, str):
            # Shorthand: "pip:requests>=2.0" or "binary:gws"
            if ":" in entry:
                dep_type, pkg = entry.split(":", 1)
            else:
                dep_type, pkg = "pip", entry
            deps.append(BootstrapDep(type=dep_type, package=pkg))
        elif isinstance(entry, dict):
            entry_d: dict[str, Any] = cast("dict[str, Any]", entry)
            deps.append(
                BootstrapDep(
                    type=entry_d.get("type", "pip"),
                    package=entry_d.get("package", ""),
                    version=entry_d.get("version", ""),
                    optional=entry_d.get("optional", False),
                ),
            )
    return BootstrapSpec(
        deps=tuple(deps),
        post_install=raw.get("post_install", ""),
        check_command=raw.get("check_command", ""),
        tools_module=raw.get("tools_module", ""),
        tools_list=raw.get("tools_list", ""),
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
            source_dir=source_path.parent if source_path else None,
            config_requirements=parse_config_requirements(data.get("config")),
            capabilities=parse_capabilities(data.get("capabilities")),
            tools=parse_tools(data.get("tools")),
            workflows=parse_workflows(data.get("workflows")),
            instructions=parse_instructions(data.get("instructions")),
            policy_hints=parse_policy_hints(data.get("policy_hints")),
            install_hook=data.get("install_hook"),
            bootstrap_hook=data.get("bootstrap_hook"),
            bootstrap=parse_bootstrap(data.get("bootstrap")),
            healthcheck=parse_healthcheck(data.get("healthcheck")),
        )
    except ManifestError:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise ManifestError(str(exc), source_path) from exc


def parse_manifest_file(path: Path) -> PluginSpec:
    """Load and parse a plugin manifest file (TOML, YAML, or JSON).

    Raises ``ManifestError`` if the file is missing or invalid.
    """
    path = Path(path)
    if not path.exists():
        msg = f"Manifest file not found: {path}"
        raise ManifestError(msg, path)

    suffix = path.suffix.lower()

    if suffix == ".toml":
        import tomllib

        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except Exception as exc:
            msg = f"Failed to parse TOML: {exc}"
            raise ManifestError(msg, path) from exc
    elif suffix in (".yaml", ".yml"):
        import warnings

        warnings.warn(
            f"YAML plugin manifests are deprecated; migrate {path.name} to TOML.",
            DeprecationWarning,
            stacklevel=2,
        )
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError:
            import json

            try:
                data = json.loads(path.read_text())
            except Exception as exc:
                msg = f"Failed to parse manifest: {exc}"
                raise ManifestError(msg, path) from exc
        else:
            try:
                data = yaml.safe_load(path.read_text())
            except Exception as exc:
                msg = f"Failed to parse YAML: {exc}"
                raise ManifestError(msg, path) from exc
    else:
        # JSON fallback
        import json

        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            msg = f"Failed to parse manifest: {exc}"
            raise ManifestError(msg, path) from exc

    if not isinstance(data, dict):
        msg = "Manifest must be a mapping"
        raise ManifestError(msg, path)

    return parse_manifest(cast("dict[str, Any]", data), source_path=path)


__all__ = [
    "ManifestError",
    "parse_manifest",
    "parse_manifest_file",
    "parse_bootstrap",
    "parse_capabilities",
    "parse_config_requirements",
    "parse_healthcheck",
    "parse_instructions",
    "parse_policy_hints",
    "parse_tools",
    "parse_workflows",
]
