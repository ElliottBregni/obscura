"""FastMCP server that proxies Obscura FastAPI endpoints as MCP tools."""

from __future__ import annotations

import contextlib
import json
from typing import Any

from fastmcp import FastMCP

from obscura.mcp_server.client import ObscuraAPIClient
from obscura.mcp_server.config import ObscuraMCPServerConfig
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level singletons (created once at import time)
# ---------------------------------------------------------------------------

_config = ObscuraMCPServerConfig.from_env()
_api = ObscuraAPIClient(_config)

mcp = FastMCP(
    name="obscura-mcp",
    instructions=(
        "Obscura MCP Server. Provides tools to interact with the Obscura "
        "multi-agent runtime: send prompts, manage agents, handle sessions, "
        "and read/write memory."
    ),
)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def _raise_tool_error(e: Exception) -> None:
    """Convert httpx errors to a re-raised ToolError."""
    import httpx as _httpx
    from fastmcp.exceptions import ToolError

    if isinstance(e, _httpx.HTTPStatusError):
        detail = e.response.text
        with contextlib.suppress(Exception):
            detail = e.response.json().get("detail", detail)
        msg = f"Obscura API error ({e.response.status_code}): {detail}"
        raise ToolError(msg)
    msg = f"Connection error: {e}"
    raise ToolError(msg)


# ---------------------------------------------------------------------------
# Prompt tools
# ---------------------------------------------------------------------------


@mcp.tool
async def send_prompt(
    prompt: str,
    backend: str = "copilot",
    model: str | None = None,
    session_id: str | None = None,
    system_prompt: str = "",
) -> str:
    """Send a prompt to the Obscura agent and get the full response.

    Args:
        prompt: The prompt text to send.
        backend: LLM backend (copilot, claude, openai, codex, localllm, moonshot).
        model: Optional model ID override.
        session_id: Optional session ID to resume a conversation.
        system_prompt: Optional system prompt.

    """
    try:
        payload: dict[str, Any] = {"prompt": prompt, "backend": backend}
        if model:
            payload["model"] = model
        if session_id:
            payload["session_id"] = session_id
        if system_prompt:
            payload["system_prompt"] = system_prompt
        result = await _api.post("/api/v1/send", json=payload)
        return result.get("text", json.dumps(result))
    except Exception as e:
        logger.debug("suppressed exception in send_prompt", exc_info=True)
        _raise_tool_error(e)
        return ""


# ---------------------------------------------------------------------------
# Session tools
# ---------------------------------------------------------------------------


@mcp.tool
async def list_sessions(backend: str | None = None) -> str:
    """List all sessions, optionally filtered by backend.

    Args:
        backend: Optional backend filter (copilot, claude, openai, etc.).

    """
    try:
        params: dict[str, Any] = {}
        if backend:
            params["backend"] = backend
        result = await _api.get("/api/v1/sessions", **params)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.debug("suppressed exception in list_sessions", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def create_session(backend: str = "copilot") -> str:
    """Create a new session.

    Args:
        backend: LLM backend for the session.

    """
    try:
        result = await _api.post("/api/v1/sessions", json={"backend": backend})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.debug("suppressed exception in create_session", exc_info=True)
        _raise_tool_error(e)
        return ""


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------


@mcp.tool
async def list_agents(
    status: str | None = None,
    tags: str | None = None,
    name: str | None = None,
) -> str:
    """List all active agents with optional filters.

    Args:
        status: Filter by status (RUNNING, IDLE, STOPPED, etc.).
        tags: Comma-separated tags to filter by.
        name: Filter by agent name (partial match).

    """
    try:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if tags:
            params["tags"] = tags
        if name:
            params["name"] = name
        result = await _api.get("/api/v1/agents", **params)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.debug("suppressed exception in list_agents", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def spawn_agent(
    name: str,
    model: str = "copilot",
    system_prompt: str = "",
    memory_namespace: str = "default",
    max_iterations: int = 10,
) -> str:
    """Spawn a new agent.

    Args:
        name: Agent name.
        model: LLM backend (copilot, claude, openai, codex, localllm, moonshot).
        system_prompt: System prompt for the agent.
        memory_namespace: Memory namespace for this agent.
        max_iterations: Maximum loop iterations.

    """
    try:
        payload: dict[str, Any] = {
            "name": name,
            "model": model,
            "system_prompt": system_prompt,
            "memory_namespace": memory_namespace,
            "max_iterations": max_iterations,
        }
        result = await _api.post("/api/v1/agents", json=payload)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.debug("suppressed exception in spawn_agent", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def get_agent(agent_id: str) -> str:
    """Get the status and details of an agent.

    Args:
        agent_id: The agent's unique ID.

    """
    try:
        result = await _api.get(f"/api/v1/agents/{agent_id}")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.debug("suppressed exception in get_agent", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def stop_agent(agent_id: str) -> str:
    """Stop and cleanup an agent.

    Args:
        agent_id: The agent's unique ID.

    """
    try:
        result = await _api.delete(f"/api/v1/agents/{agent_id}")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.debug("suppressed exception in stop_agent", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def run_agent(
    agent_id: str,
    prompt: str,
    mode: str = "run",
    timeout_seconds: float | None = None,
) -> str:
    """Run a task on an existing agent.

    Args:
        agent_id: The agent's unique ID.
        prompt: The prompt/task to execute.
        mode: Execution mode ('run' for single turn, 'loop' for iterative).
        timeout_seconds: Optional timeout in seconds.

    """
    try:
        payload: dict[str, Any] = {"prompt": prompt, "mode": mode}
        if timeout_seconds is not None:
            payload["timeout_seconds"] = timeout_seconds
        result = await _api.post(f"/api/v1/agents/{agent_id}/run", json=payload)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.debug("suppressed exception in run_agent", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def list_agent_tools(agent_id: str) -> str:
    """List the tools registered for an agent.

    Args:
        agent_id: The agent's unique ID.

    """
    try:
        result = await _api.get(f"/api/v1/agents/{agent_id}/tools")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.debug("suppressed exception in list_agent_tools", exc_info=True)
        _raise_tool_error(e)
        return ""


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------


@mcp.tool
async def get_memory(namespace: str, key: str) -> str:
    """Get a value from the memory store.

    Args:
        namespace: Memory namespace.
        key: Memory key.

    """
    try:
        result = await _api.get(f"/api/v1/memory/{namespace}/{key}")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.debug("suppressed exception in get_memory", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def set_memory(namespace: str, key: str, value: str) -> str:
    """Store a value in the memory store.

    Args:
        namespace: Memory namespace.
        key: Memory key.
        value: Value to store (string or JSON string).

    """
    try:
        result = await _api.post(
            f"/api/v1/memory/{namespace}/{key}",
            json={"value": value},
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.debug("suppressed exception in set_memory", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def delete_memory(namespace: str, key: str) -> str:
    """Delete a key from the memory store.

    Args:
        namespace: Memory namespace.
        key: Memory key.

    """
    try:
        result = await _api.delete(f"/api/v1/memory/{namespace}/{key}")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.debug("suppressed exception in delete_memory", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def list_memory_keys(namespace: str | None = None) -> str:
    """List all memory keys, optionally filtered by namespace.

    Args:
        namespace: Optional namespace filter.

    """
    try:
        params: dict[str, Any] = {}
        if namespace:
            params["namespace"] = namespace
        result = await _api.get("/api/v1/memory", **params)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.debug("suppressed exception in list_memory_keys", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def search_memory(query: str) -> str:
    """Search memory keys and values by keyword.

    Args:
        query: Search query string.

    """
    try:
        result = await _api.get("/api/v1/memory/search", q=query)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.debug("suppressed exception in search_memory", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def search_vector_memory(
    query: str,
    namespace: str | None = None,
    top_k: int = 5,
) -> str:
    """Semantic search over vector memories.

    Args:
        query: Natural language search query.
        namespace: Optional namespace filter.
        top_k: Maximum number of results to return.

    """
    try:
        params: dict[str, Any] = {"q": query, "top_k": top_k}
        if namespace:
            params["namespace"] = namespace
        result = await _api.get("/api/v1/vector-memory/search", **params)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.debug("suppressed exception in search_vector_memory", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def memory_stats() -> str:
    """Get memory usage statistics."""
    try:
        result = await _api.get("/api/v1/memory/stats")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.debug("suppressed exception in memory_stats", exc_info=True)
        _raise_tool_error(e)
        return ""


# ---------------------------------------------------------------------------
# Wizard tools
#
# These call WizardService directly rather than proxying through the FastAPI
# server. The wizard edits local files (~/.obscura/config.toml) and is a pure
# function of disk state, so the HTTP hop adds latency without buying
# anything. This also lets the MCP server run standalone (no API up).
# ---------------------------------------------------------------------------


def _wizard() -> Any:
    from obscura.wizard import WizardService

    return WizardService()


@mcp.tool
async def wizard_snapshot() -> str:
    """Read the full wizard snapshot: profiles, active state, workspace bindings, discoverables."""
    try:
        snap = _wizard().snapshot()
        return snap.model_dump_json(indent=2)
    except Exception as e:
        logger.debug("suppressed exception in wizard_snapshot", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def wizard_get_profile(name: str) -> str:
    """Return a single profile by name, or an error if not found."""
    try:
        profile = _wizard().get_profile(name)
        if profile is None:
            return json.dumps({"error": f"profile '{name}' not found"})
        return profile.model_dump_json(indent=2)
    except Exception as e:
        logger.debug("suppressed exception in wizard_get_profile", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def wizard_upsert_profile(
    name: str,
    prompts: list[str] | None = None,
    backend: str | None = None,
    model: str | None = None,
    capabilities: list[str] | None = None,
    plugins: list[str] | None = None,
    mcp_servers: list[str] | None = None,
    agents: list[str] | None = None,
) -> str:
    """Create or update a profile. Lists default to empty when omitted.

    Args:
        name: Profile identifier (used as ``[profiles.<name>]`` section key).
        prompts: Prompt files to compose into the system prompt.
        backend: Default backend (copilot, claude, openai, codex, ...).
        model: Optional model ID override.
        capabilities: Capability strings to grant.
        plugins: Plugin IDs to enable.
        mcp_servers: MCP server names to attach.
        agents: Agent names to make available.

    """
    try:
        from obscura.wizard import Profile

        profile = Profile(
            name=name,
            prompts=prompts or [],
            backend=backend,
            model=model,
            capabilities=capabilities or [],
            plugins=plugins or [],
            mcp_servers=mcp_servers or [],
            agents=agents or [],
        )
        saved = _wizard().upsert_profile(profile)
        return saved.model_dump_json(indent=2)
    except Exception as e:
        logger.debug("suppressed exception in wizard_upsert_profile", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def wizard_delete_profile(name: str) -> str:
    """Delete a profile. Returns the deleted name or an error if not found."""
    try:
        ok = _wizard().delete_profile(name)
        if not ok:
            return json.dumps({"error": f"profile '{name}' not found"})
        return json.dumps({"deleted": name})
    except Exception as e:
        logger.debug("suppressed exception in wizard_delete_profile", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def wizard_set_active(profile: str) -> str:
    """Set the active profile (default when no workspace override matches)."""
    try:
        state = _wizard().set_active(profile)
        return state.model_dump_json()
    except Exception as e:
        logger.debug("suppressed exception in wizard_set_active", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def wizard_set_workspace(path: str, profile: str) -> str:
    """Bind a working directory to a profile. ``path`` should be absolute."""
    try:
        binding = _wizard().set_workspace(path, profile)
        return binding.model_dump_json()
    except Exception as e:
        logger.debug("suppressed exception in wizard_set_workspace", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def wizard_unset_workspace(path: str) -> str:
    """Remove a workspace binding."""
    try:
        ok = _wizard().unset_workspace(path)
        if not ok:
            return json.dumps({"error": f"path '{path}' not bound"})
        return json.dumps({"unbound": path})
    except Exception as e:
        logger.debug("suppressed exception in wizard_unset_workspace", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def wizard_read_env(profile: str) -> str:
    """Read the per-profile env file (~/.obscura/.env.<profile>)."""
    try:
        content = _wizard().read_env_file(profile)
        return json.dumps({"profile": profile, "content": content})
    except Exception as e:
        logger.debug("suppressed exception in wizard_read_env", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def wizard_write_env(profile: str, content: str) -> str:
    """Write the per-profile env file. Empty content clears it. Stored 0600."""
    try:
        path = _wizard().write_env_file(profile, content)
        return json.dumps({"profile": profile, "path": str(path)})
    except Exception as e:
        logger.debug("suppressed exception in wizard_write_env", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def wizard_read_soul() -> str:
    """Read ``~/.obscura/SOUL.md`` (user's personality file)."""
    try:
        wiz = _wizard()
        return json.dumps({"path": str(wiz.soul_path()), "content": wiz.read_soul()})
    except Exception as e:
        logger.debug("suppressed exception in wizard_read_soul", exc_info=True)
        _raise_tool_error(e)
        return ""


@mcp.tool
async def wizard_write_soul(content: str) -> str:
    """Write ``~/.obscura/SOUL.md``. Atomic; empty content clears it."""
    try:
        path = _wizard().write_soul(content)
        return json.dumps({"path": str(path)})
    except Exception as e:
        logger.debug("suppressed exception in wizard_write_soul", exc_info=True)
        _raise_tool_error(e)
        return ""
