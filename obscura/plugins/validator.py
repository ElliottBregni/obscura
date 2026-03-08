"""Manifest validation rules for Obscura plugins.

Validates a parsed ``PluginSpec`` for consistency and correctness beyond
what the dataclass ``__post_init__`` checks handle. This includes:

- Tool names are unique within the plugin
- All tool capability references point to declared capabilities
- Handler references are syntactically valid (dotted paths)
- Config requirements have valid types
- Workflow required_capabilities reference declared capabilities
- No circular naming conflicts

Usage::

    from obscura.plugins.validator import validate_plugin_spec, ValidationError

    errors = validate_plugin_spec(spec)
    if errors:
        for err in errors:
            print(err)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from obscura.plugins.models import PluginSpec


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationError:
    """A single validation issue."""

    field: str
    message: str
    severity: str = "error"   # "error" | "warning"

    def __str__(self) -> str:
        return f"[{self.severity}] {self.field}: {self.message}"


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------

_HANDLER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_.]*(?::[a-zA-Z_][a-zA-Z0-9_]*)?$")
_CONFIG_TYPES = frozenset({"string", "int", "bool", "secret", "float", "list"})


def _validate_tool_names(spec: PluginSpec) -> list[ValidationError]:
    errors: list[ValidationError] = []
    seen: set[str] = set()
    for tool in spec.tools:
        if not tool.name:
            errors.append(ValidationError("tools", "Tool has empty name"))
            continue
        if tool.name in seen:
            errors.append(ValidationError(
                f"tools.{tool.name}",
                f"Duplicate tool name: {tool.name!r}",
            ))
        seen.add(tool.name)
    return errors


def _validate_tool_capabilities(spec: PluginSpec) -> list[ValidationError]:
    errors: list[ValidationError] = []
    declared_caps = {c.id for c in spec.capabilities}
    for tool in spec.tools:
        if tool.capability and tool.capability not in declared_caps:
            errors.append(ValidationError(
                f"tools.{tool.name}.capability",
                f"References undeclared capability {tool.capability!r} — "
                f"declared: {sorted(declared_caps)}",
            ))
    return errors


def _validate_capability_tools(spec: PluginSpec) -> list[ValidationError]:
    """Check that tools listed in capabilities are declared."""
    errors: list[ValidationError] = []
    declared_tools = {t.name for t in spec.tools}
    for cap in spec.capabilities:
        for tool_name in cap.tools:
            if tool_name not in declared_tools:
                errors.append(ValidationError(
                    f"capabilities.{cap.id}.tools",
                    f"References undeclared tool {tool_name!r}",
                    severity="warning",
                ))
    return errors


def _validate_handler_refs(spec: PluginSpec) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for tool in spec.tools:
        if tool.handler_ref and not _HANDLER_RE.match(tool.handler_ref):
            errors.append(ValidationError(
                f"tools.{tool.name}.handler",
                f"Invalid handler reference {tool.handler_ref!r} — "
                f"expected 'module.path:function' or 'module.path'",
            ))
    return errors


def _validate_config_requirements(spec: PluginSpec) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for cfg in spec.config_requirements:
        if not cfg.key:
            errors.append(ValidationError("config", "Config requirement has empty key"))
        if cfg.type not in _CONFIG_TYPES:
            errors.append(ValidationError(
                f"config.{cfg.key}",
                f"Unknown config type {cfg.type!r} — valid: {sorted(_CONFIG_TYPES)}",
                severity="warning",
            ))
    return errors


def _validate_workflow_capabilities(spec: PluginSpec) -> list[ValidationError]:
    errors: list[ValidationError] = []
    declared_caps = {c.id for c in spec.capabilities}
    for wf in spec.workflows:
        for cap_id in wf.required_capabilities:
            if cap_id not in declared_caps:
                errors.append(ValidationError(
                    f"workflows.{wf.id}.required_capabilities",
                    f"References undeclared capability {cap_id!r}",
                    severity="warning",
                ))
    return errors


def _validate_hooks(spec: PluginSpec) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for hook_name, hook_ref in [
        ("install_hook", spec.install_hook),
        ("bootstrap_hook", spec.bootstrap_hook),
    ]:
        if hook_ref and not _HANDLER_RE.match(hook_ref):
            errors.append(ValidationError(
                hook_name,
                f"Invalid hook reference {hook_ref!r}",
            ))
    return errors


def _validate_policy_hints(spec: PluginSpec) -> list[ValidationError]:
    errors: list[ValidationError] = []
    declared_caps = {c.id for c in spec.capabilities}
    for hint in spec.policy_hints:
        if hint.capability_id not in declared_caps:
            errors.append(ValidationError(
                f"policy_hints.{hint.capability_id}",
                f"Policy hint references undeclared capability {hint.capability_id!r}",
                severity="warning",
            ))
    return errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_plugin_spec(
    spec: PluginSpec,
    *,
    strict: bool = False,
) -> list[ValidationError]:
    """Validate a ``PluginSpec`` for consistency.

    Returns a list of ``ValidationError`` objects. An empty list means the
    spec is valid.

    If *strict* is True, warnings are promoted to errors.
    """
    errors: list[ValidationError] = []
    errors.extend(_validate_tool_names(spec))
    errors.extend(_validate_tool_capabilities(spec))
    errors.extend(_validate_capability_tools(spec))
    errors.extend(_validate_handler_refs(spec))
    errors.extend(_validate_config_requirements(spec))
    errors.extend(_validate_workflow_capabilities(spec))
    errors.extend(_validate_hooks(spec))
    errors.extend(_validate_policy_hints(spec))

    if strict:
        errors = [
            ValidationError(field=e.field, message=e.message, severity="error")
            for e in errors
        ]

    return errors


def is_valid(spec: PluginSpec, *, strict: bool = False) -> bool:
    """Return True if the spec has no errors (warnings are OK unless strict)."""
    errors = validate_plugin_spec(spec, strict=strict)
    return not any(e.severity == "error" for e in errors)


__all__ = ["validate_plugin_spec", "is_valid", "ValidationError"]
