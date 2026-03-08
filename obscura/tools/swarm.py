"""Swarm tool — enables agents to spawn sub-agents on-the-fly.

Creates a ``spawn_subagent`` ToolSpec that loads agent configs from
``~/.obscura/agents.yaml``, spawns a new agent via the runtime, runs
its full agentic loop, and returns the result as structured JSON.

Usage::

    from obscura.tools.swarm import SwarmToolContext, make_spawn_subagent_tool

    ctx = SwarmToolContext(
        runtime=runtime,
        parent_agent_id=agent.id,
        agent_configs=load_agent_configs(),
        backend="copilot",
    )
    tool_spec = make_spawn_subagent_tool(ctx)
    client.register_tool(tool_spec)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from obscura.core.types import ToolSpec

if TYPE_CHECKING:
    from obscura.agent.agents import AgentRuntime

logger = logging.getLogger(__name__)


def load_agent_configs(include_disabled: bool = False) -> dict[str, dict[str, Any]]:
    """Load agent definitions from ``~/.obscura/agents.yaml``.

    Returns a mapping of agent name → raw config dict.
    Deduplicates by name (last definition wins).
    Agents with ``enabled: false`` are excluded unless *include_disabled* is True.
    """
    agents_yaml = Path.home() / ".obscura" / "agents.yaml"
    if not agents_yaml.exists():
        return {}
    try:
        with open(agents_yaml) as f:
            data = yaml.safe_load(f)
        configs: dict[str, dict[str, Any]] = {}
        for agent_cfg in data.get("agents", []):
            name = agent_cfg.get("name")
            if not name:
                continue
            if not include_disabled and not agent_cfg.get("enabled", True):
                continue
            configs[name] = agent_cfg
        return configs
    except Exception:
        logger.warning("Failed to load agents.yaml", exc_info=True)
        return {}


def build_agent_catalog(agent_configs: dict[str, dict[str, Any]]) -> str:
    """Build a compact text catalog of available agents for LLM prompts."""
    lines: list[str] = []
    for name, cfg in agent_configs.items():
        agent_type = cfg.get("type", "loop")
        if agent_type == "daemon":
            continue  # skip daemon agents
        sp = cfg.get("system_prompt", "").strip()
        desc = sp[:120].replace("\n", " ").strip()
        if len(sp) > 120:
            desc += "..."
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


@dataclass(frozen=True)
class SwarmToolContext:
    """Context required to build the spawn_subagent tool."""

    runtime: Any  # AgentRuntime — typed as Any to avoid circular import
    parent_agent_id: str = ""
    agent_configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    backend: str = "copilot"


def _build_agent_type_list(agent_configs: dict[str, dict[str, Any]]) -> str:
    """Build a comma-separated list of available agent types for the tool description."""
    names = [
        name
        for name, cfg in agent_configs.items()
        if cfg.get("type", "loop") != "daemon"
    ]
    return ", ".join(names) if names else "assistant"


def make_spawn_subagent_tool(ctx: SwarmToolContext) -> ToolSpec:
    """Build a ``spawn_subagent`` ToolSpec wired to the given context.

    The returned tool:
    - Looks up agent_type in agents.yaml configs
    - Spawns a new agent from manifest (or generic fallback)
    - Runs its full agentic loop via ``stream_loop()``
    - Stops the agent after completion
    - Returns structured JSON with the result
    """

    async def _handler(
        agent_type: str,
        prompt: str,
        model: str = "",
    ) -> str:
        from obscura.manifest.models import AgentManifest, CapabilityConfig, PluginDepsConfig

        runtime = ctx.runtime
        if runtime is None:
            return json.dumps({"ok": False, "error": "no_runtime"})

        # Resolve agent config
        cfg = ctx.agent_configs.get(agent_type)
        agent = None

        try:
            if cfg is not None:
                s_cfg = cfg.get("skills", {})
                if not isinstance(s_cfg, dict):
                    s_cfg = {}
                raw_caps = cfg.get("capabilities", {})
                cap_cfg = CapabilityConfig(
                    grant=list(raw_caps.get("grant", [])),
                    deny=list(raw_caps.get("deny", [])),
                ) if isinstance(raw_caps, dict) else CapabilityConfig()
                plugins_cfg = cfg.get("plugins", {})
                plugin_deps = PluginDepsConfig(
                    require=list(plugins_cfg.get("require", [])),
                    optional=list(plugins_cfg.get("optional", [])),
                ) if isinstance(plugins_cfg, dict) else PluginDepsConfig()
                manifest = AgentManifest(
                    name=cfg["name"],
                    provider=model or cfg.get("model", ctx.backend),
                    system_prompt=cfg.get("system_prompt", ""),
                    max_turns=cfg.get("max_turns", 25),
                    tools=cfg.get("tools", []),
                    tags=cfg.get("tags", []),
                    mcp_servers=(
                        cfg.get("mcp_servers", [])
                        if isinstance(cfg.get("mcp_servers"), list)
                        else []
                    ),
                    skills_config=s_cfg,
                    capabilities=cap_cfg,
                    plugins=plugin_deps,
                )
                agent = runtime.spawn_from_manifest(manifest)
            else:
                # Fallback: spawn generic agent
                agent = runtime.spawn(
                    agent_type,
                    model=model or ctx.backend,
                    system_prompt=f"You are a {agent_type} specialist. Complete the task thoroughly.",
                    parent_agent_id=ctx.parent_agent_id,
                )

            await agent.start()

            # Run full agentic loop
            output_lines: list[str] = []
            async for event in agent.stream_loop(prompt):
                if hasattr(event, "text") and event.text:
                    output_lines.append(event.text)

            result_text = "".join(output_lines)
            return json.dumps({
                "ok": True,
                "agent_name": agent_type,
                "agent_id": agent.id,
                "result": result_text,
            })

        except Exception as exc:
            logger.warning(
                "spawn_subagent failed for '%s': %s", agent_type, exc,
                exc_info=True,
            )
            return json.dumps({
                "ok": False,
                "error": "spawn_failed",
                "agent_name": agent_type,
                "message": str(exc),
            })

        finally:
            if agent is not None:
                try:
                    await agent.stop()
                except Exception:
                    pass

    agent_types = _build_agent_type_list(ctx.agent_configs)

    return ToolSpec(
        name="spawn_subagent",
        description=(
            "Spawn a specialist sub-agent to handle a subtask autonomously. "
            "The sub-agent runs its full agentic loop (multi-turn, tool use) "
            "until it completes the task, then returns the result. "
            f"Available agent types: {agent_types}"
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent_type": {
                    "type": "string",
                    "description": (
                        "Agent type from agents.yaml "
                        f"(e.g. {', '.join(repr(n) for n in list(ctx.agent_configs)[:5])})"
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": "The task/prompt for the sub-agent to complete.",
                },
                "model": {
                    "type": "string",
                    "description": "Optional model override (copilot, claude, openai). Defaults to agent config.",
                },
            },
            "required": ["agent_type", "prompt"],
        },
        handler=_handler,
        required_tier="privileged",
    )
