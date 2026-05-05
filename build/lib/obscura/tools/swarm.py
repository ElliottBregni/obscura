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

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, cast

from obscura.agent.agents import AgentMessage, AgentStatus
from obscura.agent.definitions import (
    definition_to_config_dict,
    resolve_all_definitions,
)
from obscura.core.config_io import load_merged_agents
from obscura.core.enums.lifecycle import SessionStatus
from obscura.core.paths import resolve_all_obscura_homes
from obscura.core.types import ToolSpec
from obscura.manifest.models import (
    AgentManifest,
    CapabilityConfig,
    PluginDepsConfig,
)

logger = logging.getLogger(__name__)


def _empty_agent_configs() -> dict[str, dict[str, Any]]:
    return {}


def _empty_str_list() -> list[str]:
    return []


def load_agent_configs(include_disabled: bool = False) -> dict[str, dict[str, Any]]:
    """Load agent definitions from global and local agents config.

    Merges ``~/.obscura/agents.yaml`` (global) with ``.obscura/agents.yaml``
    (local).  Global agents take precedence; local configs can only **add**
    new agents, not override global ones.  Also supports ``.toml`` files as
    fallback.  Applies top-level ``defaults`` from each config file to its
    agent entries.

    Returns a mapping of agent name -> raw config dict.
    Agents with ``enabled: false`` are excluded unless *include_disabled* is True.
    """
    local_home, global_home = resolve_all_obscura_homes()

    # Global agents first (authoritative)
    configs: dict[str, dict[str, Any]] = {}
    try:
        configs.update(
            load_merged_agents(global_home, include_disabled=include_disabled),
        )
    except Exception:
        logger.warning("Failed to load agents from %s", global_home, exc_info=True)

    # Local agents only ADD new names — never override global
    if local_home.resolve() != global_home.resolve():
        try:
            local = load_merged_agents(local_home, include_disabled=include_disabled)
            for name, cfg in local.items():
                if name not in configs:
                    configs[name] = cfg
        except Exception:
            logger.warning("Failed to load agents from %s", local_home, exc_info=True)

    return configs


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
        tags = cfg.get("tags", [])
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        lines.append(f"- {name}{tag_str}: {desc}")
    return "\n".join(lines)


def match_agents_by_tags(
    query_tags: list[str],
    agent_configs: dict[str, dict[str, Any]],
) -> list[tuple[str, int, list[str]]]:
    """Match agents by tag overlap with query tags.

    Returns a list of (agent_name, match_count, matched_tags) sorted by
    match count descending.  Only agents with at least one match are returned.
    """
    query_set = {t.lower() for t in query_tags}
    matches: list[tuple[str, int, list[str]]] = []
    for name, cfg in agent_configs.items():
        if cfg.get("type", "loop") == "daemon":
            continue
        agent_tags = {t.lower() for t in cfg.get("tags", [])}
        overlap = query_set & agent_tags
        if overlap:
            matches.append((name, len(overlap), sorted(overlap)))
    matches.sort(key=lambda x: x[1], reverse=True)
    return matches


@dataclass(frozen=True)
class SwarmToolContext:
    """Context required to build the spawn_subagent tool."""

    runtime: Any  # AgentRuntime — typed as Any to avoid circular import
    parent_agent_id: str = ""
    agent_configs: dict[str, dict[str, Any]] = field(
        default_factory=_empty_agent_configs,
    )
    backend: str = "copilot"
    delegate_allowlist: list[str] = field(default_factory=_empty_str_list)
    event_store: Any = None  # EventStoreProtocol | None
    session_id: str = ""


# Background agent tracking — maps agent_id to progress info.
# Shared across all tool instances in the same process.
_background_agents: dict[str, dict[str, Any]] = {}

# Team shared memory — lightweight in-process key-value store that all
# agents in a session can read/write.  Keyed by (namespace, key).
_team_memory: dict[str, dict[str, Any]] = {}


# Agent team metrics — tracks timing and token usage per spawn.
_team_metrics: list[dict[str, Any]] = []


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
        background: bool = False,
    ) -> str:
        # Background mode: fire off the agent as an asyncio task and return
        # immediately with an agent_id handle for check_agent.
        if background:
            agent_id = f"bg-{agent_type}-{id(prompt) % 10000:04d}"
            _background_agents[agent_id] = {
                "agent_type": agent_type,
                "status": "running",
                "result": None,
                "error": None,
            }

            async def _bg_run() -> None:
                try:
                    result = await _run_one_agent(ctx, agent_type, prompt, model)
                    _background_agents[agent_id]["status"] = (
                        "completed" if result.get("ok") else "failed"
                    )
                    _background_agents[agent_id]["result"] = result
                except Exception as exc:
                    logger.debug("suppressed exception in _bg_run", exc_info=True)
                    _background_agents[agent_id]["status"] = "failed"
                    _background_agents[agent_id]["error"] = str(exc)

            asyncio.create_task(_bg_run())
            return json.dumps(
                {
                    "ok": True,
                    "background": True,
                    "agent_id": agent_id,
                    "agent_name": agent_type,
                    "message": (
                        f"Agent '{agent_type}' launched in background. "
                        f"Use check_agent(agent_id='{agent_id}') to check progress."
                    ),
                }
            )

        runtime = ctx.runtime
        if runtime is None:
            return json.dumps({"ok": False, "error": "no_runtime"})

        # Enforce delegation allowlist from parent agent
        if ctx.delegate_allowlist and agent_type not in ctx.delegate_allowlist:
            return json.dumps(
                {
                    "ok": False,
                    "error": "agent_not_allowed",
                    "agent_name": agent_type,
                    "message": (
                        f"Agent type '{agent_type}' is not in your delegation allowlist. "
                        f"Allowed agents: {ctx.delegate_allowlist}"
                    ),
                },
            )

        # Resolve agent config — try markdown definitions first, then YAML.
        cfg = None
        _definition_match = False
        try:
            defs = resolve_all_definitions()
            if agent_type in defs:
                defn = defs[agent_type]
                cfg = definition_to_config_dict(defn, parent_model=model or ctx.backend)
                cfg["name"] = defn.name
                cfg["system_prompt"] = defn.system_prompt
                cfg["tools"] = list(defn.tools)
                cfg["max_turns"] = defn.max_turns
                _definition_match = True
        except Exception:
            logger.debug("suppressed exception in _handler", exc_info=True)
        if cfg is None:
            cfg = ctx.agent_configs.get(agent_type)
        agent = None

        try:
            if cfg is not None:
                raw_skills = cfg.get("skills", {})
                s_cfg: dict[str, Any] = (
                    cast("dict[str, Any]", raw_skills)
                    if isinstance(raw_skills, dict)
                    else {}
                )
                raw_caps = cfg.get("capabilities", {})
                if isinstance(raw_caps, dict):
                    caps_dict = cast("dict[str, Any]", raw_caps)
                    cap_cfg = CapabilityConfig(
                        grant=list(caps_dict.get("grant", [])),
                        deny=list(caps_dict.get("deny", [])),
                    )
                else:
                    cap_cfg = CapabilityConfig()
                plugins_cfg = cfg.get("plugins", {})
                if isinstance(plugins_cfg, dict):
                    plugins_dict = cast("dict[str, Any]", plugins_cfg)
                    plugin_deps = PluginDepsConfig(
                        require=list(plugins_dict.get("require", [])),
                        optional=list(plugins_dict.get("optional", [])),
                    )
                else:
                    plugin_deps = PluginDepsConfig()
                raw_model_id = cfg.get("model_id")
                raw_params_unknown = cfg.get("completion_params")
                raw_params: dict[str, Any] = (
                    cast("dict[str, Any]", raw_params_unknown)
                    if isinstance(raw_params_unknown, dict)
                    else {}
                )
                raw_mcp = cfg.get("mcp_servers", [])
                mcp_list: list[Any] = (
                    cast("list[Any]", raw_mcp) if isinstance(raw_mcp, list) else []
                )
                manifest = AgentManifest(
                    name=cfg["name"],
                    provider=cfg.get("provider") or model or ctx.backend,
                    model_id=str(raw_model_id) if raw_model_id else None,
                    completion_params=raw_params,
                    system_prompt=cfg.get("system_prompt", ""),
                    max_turns=cfg.get("max_turns", 25),
                    tools=cfg.get("tools", []),
                    tags=cfg.get("tags", []),
                    mcp_servers=mcp_list,
                    skills_config=s_cfg,
                    capabilities=cap_cfg,
                    plugins=plugin_deps,
                )
                agent = runtime.spawn_from_manifest(
                    manifest,
                    parent_agent_id=ctx.parent_agent_id or None,
                )
            else:
                # Fallback: spawn generic agent
                agent = runtime.spawn(
                    agent_type,
                    model=model or ctx.backend,
                    system_prompt=f"You are a {agent_type} specialist. Complete the task thoroughly.",
                    parent_agent_id=ctx.parent_agent_id,
                )

            if ctx.event_store is not None:
                try:
                    await ctx.event_store.create_session(
                        agent.id,
                        agent_type,
                        source="subagent",
                        parent_session_id=ctx.session_id or "",
                    )
                except Exception:
                    logger.debug("suppressed exception in _handler", exc_info=True)

            await agent.start()

            # Run full agentic loop
            output_lines: list[str] = []
            async for event in agent.stream_loop(prompt):
                if hasattr(event, "text") and event.text:
                    output_lines.append(event.text)

            if ctx.event_store is not None:
                try:
                    await ctx.event_store.update_status(
                        agent.id, SessionStatus.COMPLETED
                    )
                except Exception:
                    logger.debug("suppressed exception in _handler", exc_info=True)

            result_text = "".join(output_lines)
            return json.dumps(
                {
                    "ok": True,
                    "agent_name": agent_type,
                    "agent_id": agent.id,
                    "result": result_text,
                },
            )

        except Exception as exc:
            if ctx.event_store is not None and agent is not None:
                try:
                    await ctx.event_store.update_status(agent.id, SessionStatus.FAILED)
                except Exception:
                    logger.debug("suppressed exception in _handler", exc_info=True)

            logger.warning(
                "spawn_subagent failed for '%s': %s",
                agent_type,
                exc,
                exc_info=True,
            )
            return json.dumps(
                {
                    "ok": False,
                    "error": "spawn_failed",
                    "agent_name": agent_type,
                    "message": str(exc),
                },
            )

        finally:
            if agent is not None:
                with contextlib.suppress(Exception):
                    await agent.stop()

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
                "background": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Run in background. Returns immediately with an agent_id handle. "
                        "Use check_agent to poll for results."
                    ),
                },
            },
            "required": ["agent_type", "prompt"],
        },
        handler=_handler,
    )


# ---------------------------------------------------------------------------
# spawn_agents — batch concurrent dispatch
# ---------------------------------------------------------------------------


async def _run_one_agent(
    ctx: SwarmToolContext,
    agent_type: str,
    prompt: str,
    model: str = "",
) -> dict[str, Any]:
    """Spawn a single agent, run to completion, return result dict."""
    started_at = time.monotonic()

    runtime = ctx.runtime
    if runtime is None:
        return {"ok": False, "error": "no_runtime", "agent_name": agent_type}

    if ctx.delegate_allowlist and agent_type not in ctx.delegate_allowlist:
        return {
            "ok": False,
            "error": "agent_not_allowed",
            "agent_name": agent_type,
            "message": f"Not in allowlist. Allowed: {ctx.delegate_allowlist}",
        }

    cfg = None
    try:
        defs = resolve_all_definitions()
        if agent_type in defs:
            defn = defs[agent_type]
            cfg = definition_to_config_dict(defn, parent_model=model or ctx.backend)
            cfg["name"] = defn.name
            cfg["system_prompt"] = defn.system_prompt
            cfg["tools"] = list(defn.tools)
            cfg["max_turns"] = defn.max_turns
    except Exception:
        logger.debug("suppressed exception in _run_one_agent", exc_info=True)
    if cfg is None:
        cfg = ctx.agent_configs.get(agent_type)

    agent = None
    try:
        if cfg is not None:
            raw_skills = cfg.get("skills", {})
            s_cfg: dict[str, Any] = (
                cast("dict[str, Any]", raw_skills)
                if isinstance(raw_skills, dict)
                else {}
            )
            raw_caps = cfg.get("capabilities", {})
            if isinstance(raw_caps, dict):
                caps_dict = cast("dict[str, Any]", raw_caps)
                cap_cfg = CapabilityConfig(
                    grant=list(caps_dict.get("grant", [])),
                    deny=list(caps_dict.get("deny", [])),
                )
            else:
                cap_cfg = CapabilityConfig()
            plugins_cfg = cfg.get("plugins", {})
            if isinstance(plugins_cfg, dict):
                plugins_dict = cast("dict[str, Any]", plugins_cfg)
                plugin_deps = PluginDepsConfig(
                    require=list(plugins_dict.get("require", [])),
                    optional=list(plugins_dict.get("optional", [])),
                )
            else:
                plugin_deps = PluginDepsConfig()
            raw_model_id = cfg.get("model_id")
            raw_params_unknown = cfg.get("completion_params")
            raw_params: dict[str, Any] = (
                cast("dict[str, Any]", raw_params_unknown)
                if isinstance(raw_params_unknown, dict)
                else {}
            )
            raw_mcp = cfg.get("mcp_servers", [])
            mcp_list: list[Any] = (
                cast("list[Any]", raw_mcp) if isinstance(raw_mcp, list) else []
            )
            manifest = AgentManifest(
                name=cfg["name"],
                provider=cfg.get("provider") or model or ctx.backend,
                model_id=str(raw_model_id) if raw_model_id else None,
                completion_params=raw_params,
                system_prompt=cfg.get("system_prompt", ""),
                max_turns=cfg.get("max_turns", 25),
                tools=cfg.get("tools", []),
                tags=cfg.get("tags", []),
                mcp_servers=mcp_list,
                skills_config=s_cfg,
                capabilities=cap_cfg,
                plugins=plugin_deps,
            )
            agent = runtime.spawn_from_manifest(
                manifest,
                parent_agent_id=ctx.parent_agent_id or None,
            )
        else:
            agent = runtime.spawn(
                agent_type,
                model=model or ctx.backend,
                system_prompt=f"You are a {agent_type} specialist. Complete the task thoroughly.",
                parent_agent_id=ctx.parent_agent_id,
            )

        await agent.start()
        output_lines: list[str] = []
        async for event in agent.stream_loop(prompt):
            if hasattr(event, "text") and event.text:
                output_lines.append(event.text)

        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        _team_metrics.append(
            {
                "agent_name": agent_type,
                "agent_id": agent.id,
                "status": "completed",
                "duration_ms": elapsed_ms,
                "result_length": len("".join(output_lines)),
                "iteration_count": agent.iteration_count,
            }
        )

        return {
            "ok": True,
            "agent_name": agent_type,
            "agent_id": agent.id,
            "result": "".join(output_lines),
            "duration_ms": elapsed_ms,
        }
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        _team_metrics.append(
            {
                "agent_name": agent_type,
                "agent_id": getattr(agent, "id", "unknown") if agent else "unknown",
                "status": "failed",
                "duration_ms": elapsed_ms,
                "error": str(exc),
            }
        )
        logger.warning("Agent '%s' failed: %s", agent_type, exc, exc_info=True)
        return {
            "ok": False,
            "error": "spawn_failed",
            "agent_name": agent_type,
            "message": str(exc),
            "duration_ms": elapsed_ms,
        }
    finally:
        if agent is not None:
            with contextlib.suppress(Exception):
                await agent.stop()


def make_spawn_agents_tool(ctx: SwarmToolContext) -> ToolSpec:
    """Build a ``spawn_agents`` ToolSpec for concurrent multi-agent dispatch.

    Accepts a JSON array of ``{agent_type, prompt, model?}`` and runs
    **all** concurrently via ``asyncio.gather``.
    """

    async def _handler(agents: list[dict[str, str]]) -> str:
        if not agents:
            return json.dumps({"ok": False, "error": "empty_agent_list"})

        coros = [
            _run_one_agent(
                ctx,
                agent_type=spec.get("agent_type", "general-purpose"),
                prompt=spec.get("prompt", ""),
                model=spec.get("model", ""),
            )
            for spec in agents
        ]
        results = await asyncio.gather(*coros)
        return json.dumps({"ok": True, "results": list(results)})

    agent_types = _build_agent_type_list(ctx.agent_configs)

    return ToolSpec(
        name="spawn_agents",
        description=(
            "Spawn MULTIPLE sub-agents concurrently and wait for all results. "
            "Much faster than calling spawn_subagent multiple times — "
            "all agents run in parallel. "
            f"Available agent types: {agent_types}"
        ),
        parameters={
            "type": "object",
            "properties": {
                "agents": {
                    "type": "array",
                    "description": "List of agent tasks to run concurrently.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent_type": {
                                "type": "string",
                                "description": "Agent type to spawn.",
                            },
                            "prompt": {
                                "type": "string",
                                "description": "Task for this agent.",
                            },
                            "model": {
                                "type": "string",
                                "description": "Optional model override.",
                            },
                        },
                        "required": ["agent_type", "prompt"],
                    },
                },
            },
            "required": ["agents"],
        },
        handler=_handler,
    )


# ---------------------------------------------------------------------------
# send_message — lightweight teammate addressing
# ---------------------------------------------------------------------------


def make_send_message_tool(ctx: SwarmToolContext) -> ToolSpec:
    """Build a ``send_message`` ToolSpec for agent-to-agent communication.

    Lighter than ``delegate_to_agent`` — sends a message to an already
    running peer, avoiding a full agent spawn cycle.
    """

    async def _handler(
        to: str,
        message: str,
        mode: str = "request",
    ) -> str:
        runtime = ctx.runtime
        if runtime is None:
            return json.dumps({"ok": False, "error": "no_runtime"})

        # Resolve target by name or ID
        target_agent = None
        for agent in runtime.agents.values():
            if to in (agent.config.name, agent.id):
                target_agent = agent
                break

        if target_agent is None:
            available = [
                a.config.name
                for a in runtime.agents.values()
                if a.status in (AgentStatus.RUNNING, AgentStatus.WAITING)
            ]
            return json.dumps(
                {
                    "ok": False,
                    "error": "agent_not_found",
                    "message": f"No running agent named '{to}'. Available: {available}",
                },
            )

        if mode == "fire_and_forget":
            msg = AgentMessage(
                source=ctx.parent_agent_id or "coordinator",
                target=target_agent.id,
                content=message,
                message_type="text",
            )
            target_agent.enqueue_message(msg)
            return json.dumps(
                {
                    "ok": True,
                    "mode": "fire_and_forget",
                    "target": to,
                    "status": "enqueued",
                },
            )

        # Request mode: invoke peer loop
        try:
            result = await runtime.invoke_peer(
                runtime.peer_registry.resolve(target_agent.id),
                message,
                use_loop=True,
            )
            return json.dumps(
                {
                    "ok": True,
                    "mode": "request",
                    "target": to,
                    "result": result,
                },
            )
        except Exception as exc:
            logger.debug("suppressed exception in _handler", exc_info=True)
            return json.dumps(
                {
                    "ok": False,
                    "error": type(exc).__name__,
                    "target": to,
                    "message": str(exc),
                },
            )

    agent_names = sorted(
        name
        for name, cfg in ctx.agent_configs.items()
        if cfg.get("type", "loop") != "daemon"
    )

    return ToolSpec(
        name="send_message",
        description=(
            "Send a message to a running teammate agent by name or ID. "
            "Use mode='request' (default) to wait for a response, or "
            "'fire_and_forget' to send without waiting. "
            f"Known agents: {', '.join(agent_names[:10])}"
        ),
        parameters={
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Name or ID of the target agent.",
                },
                "message": {
                    "type": "string",
                    "description": "The message to send.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["request", "fire_and_forget"],
                    "default": "request",
                    "description": "Delivery mode.",
                },
            },
            "required": ["to", "message"],
        },
        handler=_handler,
    )


# ---------------------------------------------------------------------------
# check_agent — poll background agent status
# ---------------------------------------------------------------------------


def make_check_agent_tool() -> ToolSpec:
    """Build a ``check_agent`` ToolSpec for polling background agent status.

    Returns the current status of a backgrounded agent launched via
    ``spawn_subagent(background=true)``.  If the agent is done, returns
    the full result.
    """

    async def _handler(agent_id: str) -> str:
        info = _background_agents.get(agent_id)
        if info is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": "not_found",
                    "message": (
                        f"No background agent with id '{agent_id}'. "
                        f"Active: {list(_background_agents.keys())}"
                    ),
                }
            )

        status = info["status"]
        response: dict[str, Any] = {
            "ok": True,
            "agent_id": agent_id,
            "agent_type": info["agent_type"],
            "status": status,
        }

        if status == "completed" and info.get("result"):
            response["result"] = info["result"]
        elif status == "failed":
            response["error"] = info.get("error") or info.get("result", {}).get(
                "message"
            )

        return json.dumps(response)

    return ToolSpec(
        name="check_agent",
        description=(
            "Check the status of a background agent launched via "
            "spawn_subagent(background=true). Returns status and result "
            "if the agent has completed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The agent_id returned by spawn_subagent in background mode.",
                },
            },
            "required": ["agent_id"],
        },
        handler=_handler,
    )


# ---------------------------------------------------------------------------
# suggest_agents — tag-based routing
# ---------------------------------------------------------------------------


def make_suggest_agents_tool(ctx: SwarmToolContext) -> ToolSpec:
    """Build a ``suggest_agents`` ToolSpec for tag-based agent routing.

    Given a list of keywords/tags describing a task, returns ranked
    agent suggestions based on tag overlap.
    """

    async def _handler(tags: list[str]) -> str:
        if not tags:
            return json.dumps({"ok": False, "error": "no_tags_provided"})

        matches = match_agents_by_tags(tags, ctx.agent_configs)
        if not matches:
            all_agents = [
                {"name": name, "tags": cfg.get("tags", [])}
                for name, cfg in ctx.agent_configs.items()
                if cfg.get("type", "loop") != "daemon"
            ]
            return json.dumps(
                {
                    "ok": True,
                    "matches": [],
                    "message": "No tag matches found.",
                    "all_agents": all_agents,
                }
            )

        return json.dumps(
            {
                "ok": True,
                "matches": [
                    {"agent": name, "match_count": count, "matched_tags": matched}
                    for name, count, matched in matches
                ],
            }
        )

    return ToolSpec(
        name="suggest_agents",
        description=(
            "Find the best agent for a task by matching keywords/tags. "
            "Pass task-related tags (e.g. ['python', 'testing', 'security']) "
            "and get ranked agent suggestions based on tag overlap."
        ),
        parameters={
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Keywords/tags describing the task.",
                },
            },
            "required": ["tags"],
        },
        handler=_handler,
    )


# ---------------------------------------------------------------------------
# Team shared memory
# ---------------------------------------------------------------------------


def make_team_memory_write_tool() -> ToolSpec:
    """Build a ``team_memory_write`` tool for shared agent-to-agent state."""

    async def _handler(key: str, value: Any, namespace: str = "default") -> str:
        ns_key = f"{namespace}:{key}"
        _team_memory[ns_key] = {
            "value": value,
            "written_at": time.time(),
            "namespace": namespace,
            "key": key,
        }
        return json.dumps({"ok": True, "key": ns_key})

    return ToolSpec(
        name="team_memory_write",
        description=(
            "Write a key-value pair to shared team memory. "
            "All agents in this session can read it. Use to share "
            "intermediate results, findings, or coordination state."
        ),
        parameters={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Memory key (e.g. 'search_results', 'findings').",
                },
                "value": {
                    "description": "Any JSON-serializable value to store.",
                },
                "namespace": {
                    "type": "string",
                    "default": "default",
                    "description": "Optional namespace for grouping.",
                },
            },
            "required": ["key", "value"],
        },
        handler=_handler,
    )


def make_team_memory_read_tool() -> ToolSpec:
    """Build a ``team_memory_read`` tool for reading shared state."""

    async def _handler(key: str = "", namespace: str = "default") -> str:
        if key:
            ns_key = f"{namespace}:{key}"
            entry = _team_memory.get(ns_key)
            if entry is None:
                return json.dumps({"ok": False, "error": "not_found", "key": ns_key})
            return json.dumps({"ok": True, "key": ns_key, "entry": entry})

        # No key: list all entries in namespace
        entries = {k: v for k, v in _team_memory.items() if v["namespace"] == namespace}
        return json.dumps({"ok": True, "namespace": namespace, "entries": entries})

    return ToolSpec(
        name="team_memory_read",
        description=(
            "Read from shared team memory. Pass a key to read one entry, "
            "or omit key to list all entries in a namespace."
        ),
        parameters={
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Key to read. Omit to list all keys.",
                },
                "namespace": {
                    "type": "string",
                    "default": "default",
                    "description": "Namespace to read from.",
                },
            },
        },
        handler=_handler,
    )


# ---------------------------------------------------------------------------
# Team status / metrics
# ---------------------------------------------------------------------------


def make_team_status_tool() -> ToolSpec:
    """Build a ``team_status`` tool for viewing agent team metrics."""

    async def _handler() -> str:
        if not _team_metrics:
            return json.dumps(
                {
                    "ok": True,
                    "message": "No agents have been spawned yet.",
                    "agents_spawned": 0,
                }
            )

        total = len(_team_metrics)
        completed = [m for m in _team_metrics if m["status"] == "completed"]
        failed = [m for m in _team_metrics if m["status"] == "failed"]

        total_duration_ms = sum(m.get("duration_ms", 0) for m in _team_metrics)
        max_duration_ms = max(
            (m.get("duration_ms", 0) for m in _team_metrics), default=0
        )

        # Per-agent breakdown
        by_agent: dict[str, list[dict[str, Any]]] = {}
        for m in _team_metrics:
            name = m["agent_name"]
            by_agent.setdefault(name, []).append(m)

        agent_summary = {}
        for name, runs in by_agent.items():
            agent_summary[name] = {
                "runs": len(runs),
                "completed": sum(1 for r in runs if r["status"] == "completed"),
                "failed": sum(1 for r in runs if r["status"] == "failed"),
                "avg_duration_ms": int(
                    sum(r.get("duration_ms", 0) for r in runs) / len(runs)
                ),
                "total_iterations": sum(r.get("iteration_count", 0) for r in runs),
            }

        # Shared memory size
        memory_keys = len(_team_memory)
        bg_agents = len(_background_agents)

        return json.dumps(
            {
                "ok": True,
                "agents_spawned": total,
                "completed": len(completed),
                "failed": len(failed),
                "total_duration_ms": total_duration_ms,
                "max_duration_ms": max_duration_ms,
                "parallel_speedup": (
                    f"{total_duration_ms / max_duration_ms:.1f}x"
                    if max_duration_ms > 0
                    else "N/A"
                ),
                "agent_breakdown": agent_summary,
                "shared_memory_keys": memory_keys,
                "background_agents": bg_agents,
            }
        )

    return ToolSpec(
        name="team_status",
        description=(
            "View agent team metrics: spawned count, success/failure rates, "
            "timing breakdown per agent type, parallel speedup estimate, "
            "and shared memory usage."
        ),
        parameters={"type": "object", "properties": {}},
        handler=_handler,
    )
