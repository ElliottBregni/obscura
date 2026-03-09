"""obscura.core.compiler.migrate — Legacy agents.yaml to spec converter.

Reads the flat agents.yaml format and generates Template spec YAML files
that can be loaded by the compiler pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class MigrationResult:
    """Summary of a migration run."""

    templates_written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def migrate_agents_yaml(
    agents_yaml: Path,
    output_dir: Path,
    *,
    overwrite: bool = False,
) -> MigrationResult:
    """Convert flat agents.yaml entries into Template spec YAML files.

    Parameters
    ----------
    agents_yaml:
        Path to the legacy agents.yaml file.
    output_dir:
        Directory to write generated template YAMLs (typically specs/templates/).
    overwrite:
        If True, overwrite existing template files.

    Returns
    -------
    MigrationResult
        Summary of what was migrated.
    """
    result = MigrationResult()

    if not agents_yaml.is_file():
        result.errors.append(f"agents.yaml not found: {agents_yaml}")
        return result

    try:
        raw = yaml.safe_load(agents_yaml.read_text(encoding="utf-8"))
    except Exception as exc:
        result.errors.append(f"Failed to parse {agents_yaml}: {exc}")
        return result

    if not isinstance(raw, dict) or "agents" not in raw:
        result.errors.append("Invalid agents.yaml: expected top-level 'agents' key")
        return result

    agents: list[dict[str, Any]] = raw["agents"]
    output_dir.mkdir(parents=True, exist_ok=True)

    for agent in agents:
        if not isinstance(agent, dict):
            continue

        name = agent.get("name")
        if not name:
            result.errors.append("Agent entry missing 'name' field")
            continue

        # Check if agent is disabled
        if not agent.get("enabled", True):
            result.skipped.append(f"{name} (disabled)")
            continue

        dest = output_dir / f"{name}.yml"
        if dest.exists() and not overwrite:
            result.skipped.append(f"{name} (exists)")
            continue

        try:
            template_yaml = _agent_to_template_yaml(agent)
            dest.write_text(template_yaml, encoding="utf-8")
            result.templates_written.append(name)
        except Exception as exc:
            result.errors.append(f"{name}: {exc}")

    return result


def _agent_to_template_yaml(agent: dict[str, Any]) -> str:
    """Convert a single flat agent dict to a Template spec YAML string."""
    name: str = agent["name"]
    tags: list[str] = agent.get("tags", [])

    # Map flat fields to template spec fields
    provider = agent.get("provider", "copilot")
    model_id = agent.get("model_id")
    agent_type = agent.get("type", "loop")
    max_iterations = agent.get("max_turns", 25)
    system_prompt = agent.get("system_prompt", "")

    # Extract plugins from the plugins dict
    plugins: list[str] = []
    plugins_raw = agent.get("plugins", {})
    if isinstance(plugins_raw, dict):
        for p in plugins_raw.get("require", []):
            if p and p not in plugins:
                plugins.append(p)
        for p in plugins_raw.get("optional", []):
            if p and p not in plugins:
                plugins.append(p)
    elif isinstance(plugins_raw, list):
        plugins = [p for p in plugins_raw if p]

    # Extract capabilities
    capabilities: list[str] = []
    caps_raw = agent.get("capabilities", {})
    if isinstance(caps_raw, dict):
        capabilities = caps_raw.get("grant", [])
    elif isinstance(caps_raw, list):
        capabilities = caps_raw

    # Extract tool permissions into allowlist/denylist
    tool_allowlist: list[str] | None = None
    tool_denylist: list[str] = []
    perms = agent.get("permissions", {})
    if isinstance(perms, dict):
        allow = perms.get("allow", [])
        deny = perms.get("deny", [])
        if allow:
            tool_allowlist = allow
        if deny:
            tool_denylist = deny

    # Build extra config for fields that don't map directly
    config: dict[str, Any] = {}
    if agent.get("timeout_seconds"):
        config["timeout_seconds"] = agent["timeout_seconds"]
    if agent.get("memory_namespace"):
        config["memory_namespace"] = agent["memory_namespace"]
    if agent.get("can_delegate"):
        config["can_delegate"] = True
        if agent.get("delegate_allowlist"):
            config["delegate_allowlist"] = agent["delegate_allowlist"]
        if agent.get("max_delegation_depth"):
            config["max_delegation_depth"] = agent["max_delegation_depth"]

    # Build the spec dict
    spec: dict[str, Any] = {}
    spec["provider"] = provider
    if model_id:
        spec["model_id"] = model_id
    spec["agent_type"] = agent_type
    spec["max_iterations"] = max_iterations
    if plugins:
        spec["plugins"] = plugins
    if capabilities:
        spec["capabilities"] = capabilities
    if tool_allowlist is not None:
        spec["tool_allowlist"] = tool_allowlist
    if tool_denylist:
        spec["tool_denylist"] = tool_denylist
    if system_prompt:
        spec["instructions"] = system_prompt
    if config:
        spec["config"] = config

    # Build the full template document
    doc: dict[str, Any] = {
        "apiVersion": "obscura/v1",
        "kind": "Template",
        "metadata": {"name": name},
        "spec": spec,
    }
    if tags:
        doc["metadata"]["tags"] = tags

    return yaml.dump(
        doc, default_flow_style=False, sort_keys=False, allow_unicode=True
    )
