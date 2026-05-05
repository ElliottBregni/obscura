"""obscura.core.compiler.migrate — Legacy agents config to spec converter.

Reads the flat agents.yaml/toml format and generates Template spec TOML files
that can be loaded by the compiler pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from obscura.core.config_io import apply_agent_defaults, dumps_toml, load_config

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def _empty_str_list() -> list[str]:
    return []


@dataclass
class MigrationResult:
    """Summary of a migration run."""

    templates_written: list[str] = field(default_factory=_empty_str_list)
    skipped: list[str] = field(default_factory=_empty_str_list)
    errors: list[str] = field(default_factory=_empty_str_list)


def migrate_agents_yaml(
    agents_yaml: Path,
    output_dir: Path,
    *,
    overwrite: bool = False,
) -> MigrationResult:
    """Convert flat agents config entries into Template spec TOML files.

    Parameters
    ----------
    agents_yaml:
        Path to the legacy agents config file (.toml or .yaml).
    output_dir:
        Directory to write generated template TOMLs (typically specs/templates/).
    overwrite:
        If True, overwrite existing template files.

    Returns
    -------
    MigrationResult
        Summary of what was migrated.

    """
    result = MigrationResult()

    if not agents_yaml.is_file():
        result.errors.append(f"Agents config not found: {agents_yaml}")
        return result

    try:
        raw = load_config(agents_yaml, warn_yaml=False)
    except Exception as exc:
        logger.debug("suppressed exception in migrate_agents_yaml", exc_info=True)
        result.errors.append(f"Failed to parse {agents_yaml}: {exc}")
        return result

    raw = apply_agent_defaults(raw)

    if "agents" not in raw:
        result.errors.append("Invalid agents config: expected top-level 'agents' key")
        return result

    agents_raw = raw["agents"]
    if not isinstance(agents_raw, list):
        result.errors.append("Invalid agents config: 'agents' must be a list")
        return result
    agents = cast(list[Any], agents_raw)
    output_dir.mkdir(parents=True, exist_ok=True)

    for agent_raw in agents:
        if not isinstance(agent_raw, dict):
            continue
        agent = cast(dict[str, Any], agent_raw)

        name = agent.get("name")
        if not name:
            result.errors.append("Agent entry missing 'name' field")
            continue

        # Check if agent is disabled
        if not agent.get("enabled", True):
            result.skipped.append(f"{name} (disabled)")
            continue

        dest = output_dir / f"{name}.toml"
        if dest.exists() and not overwrite:
            result.skipped.append(f"{name} (exists)")
            continue

        try:
            template_toml = _agent_to_template_toml(agent)
            dest.write_text(template_toml, encoding="utf-8")
            result.templates_written.append(name)
        except Exception as exc:
            logger.debug("suppressed exception in migrate_agents_yaml", exc_info=True)
            result.errors.append(f"{name}: {exc}")

    return result


def _agent_to_template_toml(agent: dict[str, Any]) -> str:
    """Convert a single flat agent dict to a Template spec TOML string."""
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
        plugins_dict = cast(dict[str, Any], plugins_raw)
        for p in cast(list[Any], plugins_dict.get("require", [])):
            if p and isinstance(p, str) and p not in plugins:
                plugins.append(p)
        for p in cast(list[Any], plugins_dict.get("optional", [])):
            if p and isinstance(p, str) and p not in plugins:
                plugins.append(p)
    elif isinstance(plugins_raw, list):
        plugins = [str(p) for p in cast(list[Any], plugins_raw) if p]

    # Extract capabilities
    capabilities: list[str] = []
    caps_raw = agent.get("capabilities", {})
    if isinstance(caps_raw, dict):
        grant = cast(dict[str, Any], caps_raw).get("grant", [])
        if isinstance(grant, list):
            capabilities = [str(c) for c in cast(list[Any], grant)]
    elif isinstance(caps_raw, list):
        capabilities = [str(c) for c in cast(list[Any], caps_raw)]

    # Extract tool permissions into allowlist/denylist
    tool_allowlist: list[str] | None = None
    tool_denylist: list[str] = []
    perms = agent.get("permissions", {})
    if isinstance(perms, dict):
        perms_dict = cast(dict[str, Any], perms)
        allow = perms_dict.get("allow", [])
        deny = perms_dict.get("deny", [])
        if isinstance(allow, list) and allow:
            tool_allowlist = [str(x) for x in cast(list[Any], allow)]
        if isinstance(deny, list) and deny:
            tool_denylist = [str(x) for x in cast(list[Any], deny)]

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

    return dumps_toml(doc)
