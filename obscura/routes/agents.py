"""Routes: agent CRUD, bulk ops, templates, tags, streaming."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any, AsyncGenerator, cast

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from obscura.auth.models import AuthenticatedUser
from obscura.auth.rbac import AGENT_READ_ROLES, AGENT_WRITE_ROLES, require_any_role
from obscura.deps import audit, get_runtime
from obscura.core.paths import resolve_obscura_mcp_dir

router = APIRouter(prefix="/api/v1", tags=["agents"])

# In-memory template store
agent_templates: dict[str, dict[str, Any]] = {}


def get_agent_templates() -> dict[str, dict[str, Any]]:
    """Read-only access to agent templates (for admin stats/tests)."""
    return agent_templates


def clear_agent_templates() -> None:
    """Clear agent templates (testing helper)."""
    agent_templates.clear()


def get_agent_templates_view() -> dict[str, dict[str, Any]]:
    """Return a shallow copy for safe read access."""
    return dict(agent_templates)


# -- CRUD -----------------------------------------------------------------


@router.post("/agents")
async def agent_spawn(
    body: dict[str, Any],
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Spawn a new agent."""
    model: str = body.get("model", "copilot")
    valid_models = ("copilot", "claude", "localllm", "openai", "moonshot")
    if model not in valid_models:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid model '{model}'. Must be one of: {valid_models}",
        )

    runtime = await get_runtime(user)

    mcp_config: dict[str, Any] = body.get("mcp", {})
    mcp_enabled: bool = mcp_config.get("enabled", False)
    mcp_servers: list[dict[str, Any]] = mcp_config.get("servers", [])
    mcp_config_path = str(
        mcp_config.get("config_path", str(resolve_obscura_mcp_dir()))
    )
    raw_server_names = mcp_config.get("server_names", [])
    mcp_server_names: list[str] = (
        [str(name) for name in cast(list[Any], raw_server_names)]
        if isinstance(raw_server_names, list)
        else []
    )
    mcp_primary_server_name = str(mcp_config.get("primary_server_name", "github"))
    mcp_auto_discover = bool(mcp_config.get("auto_discover", True))
    mcp_resolve_env = bool(mcp_config.get("resolve_env", True))

    from obscura.agent.agents import MCPConfig
    raw_a2a_remote_tools = body.get("a2a_remote_tools", {})
    a2a_remote_tools: dict[str, Any] = (
        cast(dict[str, Any], raw_a2a_remote_tools)
        if isinstance(raw_a2a_remote_tools, dict)
        else {}
    )

    agent = runtime.spawn(
        name=body.get("name", "unnamed"),
        model=model,
        system_prompt=body.get("system_prompt", ""),
        memory_namespace=body.get("memory_namespace", "default"),
        max_iterations=body.get("max_iterations", 10),
        enable_system_tools=bool(body.get("enable_system_tools", True)),
        a2a_remote_tools=a2a_remote_tools,
        mcp=MCPConfig(
            enabled=mcp_enabled,
            servers=mcp_servers,
            config_path=mcp_config_path,
            server_names=mcp_server_names,
            primary_server_name=mcp_primary_server_name,
            auto_discover=mcp_auto_discover,
            resolve_env=mcp_resolve_env,
        ),
    )

    await agent.start()

    audit(
        "agent.spawn",
        user,
        f"agent:{agent.id}",
        "create",
        "success",
        name=agent.config.name,
        model=agent.config.model,
        mcp_enabled=mcp_enabled,
    )

    return JSONResponse(
        content={
            "agent_id": agent.id,
            "name": agent.config.name,
            "status": agent.status.name,
            "created_at": agent.created_at.isoformat(),
            "mcp_enabled": mcp_enabled,
        }
    )


@router.get("/agents/{agent_id}")
async def agent_get(
    agent_id: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Get agent status and details."""
    runtime = await get_runtime(user)
    state = runtime.get_agent_status(agent_id)

    if state is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    return JSONResponse(
        content={
            "agent_id": state.agent_id,
            "name": state.name,
            "status": state.status.name,
            "created_at": state.created_at.isoformat(),
            "updated_at": state.updated_at.isoformat(),
            "iteration_count": state.iteration_count,
            "error_message": state.error_message,
        }
    )


@router.get("/agents/{agent_id}/tools")
async def agent_list_tools(
    agent_id: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """List currently registered tools for an agent."""
    runtime = await get_runtime(user)
    agent = runtime.get_agent(agent_id)

    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    tools = agent.list_registered_tools()
    return JSONResponse(
        content={
            "agent_id": agent_id,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "required_tier": tool.required_tier,
                    "parameters": tool.parameters,
                }
                for tool in tools
            ],
        }
    )


@router.post("/agents/{agent_id}/run")
async def agent_run(
    agent_id: str,
    body: dict[str, Any],
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Run a task on an existing agent."""
    runtime = await get_runtime(user)
    agent = runtime.get_agent(agent_id)

    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    prompt: str = body.get("prompt", "")
    context: dict[str, Any] = body.get("context", {})
    timeout_raw = body.get("timeout_seconds")
    timeout_seconds: float | None = None
    if timeout_raw is not None:
        try:
            timeout_seconds = float(timeout_raw)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="timeout_seconds must be a positive number",
            )
        if timeout_seconds <= 0:
            raise HTTPException(
                status_code=400,
                detail="timeout_seconds must be a positive number",
            )

    try:
        if timeout_seconds is None:
            result = await agent.run(prompt, **context)
        else:
            run_task = asyncio.create_task(
                agent.run(prompt, **context)
            )
            done, _pending = await asyncio.wait(
                {run_task},
                timeout=timeout_seconds,
            )
            if run_task not in done:
                run_task.cancel()
                raise HTTPException(
                    status_code=504,
                    detail=f"Agent run timed out after {timeout_seconds:.3f}s",
                )
            result = await run_task
        return JSONResponse(
            content={
                "agent_id": agent_id,
                "status": agent.status.name,
                "result": result,
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/agents/{agent_id}")
async def agent_stop(
    agent_id: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Stop and cleanup an agent."""
    runtime = await get_runtime(user)
    agent = runtime.get_agent(agent_id)

    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    await agent.stop()
    audit("agent.stop", user, f"agent:{agent_id}", "stop", "success")

    return JSONResponse(
        content={
            "agent_id": agent_id,
            "status": "stopped",
        }
    )


@router.get("/agents")
async def agent_list(
    status: str | None = None,
    tags: str | None = None,
    name: str | None = None,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """List all agents for the user."""
    from obscura.agent.agents import AgentStatus

    runtime = await get_runtime(user)

    status_filter = None
    if status:
        try:
            status_filter = AgentStatus[status.upper()]
        except KeyError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    agents = runtime.list_agents(status=status_filter)

    if tags:
        tag_list = [t.strip() for t in tags.split(",")]
        agents = [
            a
            for a in agents
            if any(t in getattr(a.config, "tags", []) for t in tag_list)
        ]

    if name:
        agents = [a for a in agents if name.lower() in a.config.name.lower()]

    return JSONResponse(
        content={
            "agents": [
                {
                    "agent_id": a.id,
                    "name": a.config.name,
                    "status": a.status.name,
                    "model": a.config.model,
                    "tags": getattr(a.config, "tags", []),
                    "created_at": a.created_at.isoformat(),
                }
                for a in agents
            ],
            "count": len(agents),
        }
    )


# -- bulk operations -------------------------------------------------------


@router.post("/agents/bulk")
async def agents_bulk_spawn(
    body: dict[str, Any],
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Spawn multiple agents in one request."""
    runtime = await get_runtime(user)
    agents_config: list[dict[str, Any]] = body.get("agents", [])

    if not agents_config:
        raise HTTPException(status_code=400, detail="No agents provided")
    if len(agents_config) > 100:
        raise HTTPException(
            status_code=400, detail="Cannot spawn more than 100 agents at once"
        )

    created: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for idx, cfg in enumerate(agents_config):
        try:
            agent = runtime.spawn(
                name=cfg.get("name", f"bulk-agent-{idx}"),
                model=cfg.get("model", "claude"),
                system_prompt=cfg.get("system_prompt", ""),
                memory_namespace=cfg.get("memory_namespace", "default"),
                max_iterations=cfg.get("max_iterations", 10),
                tags=cfg.get("tags", []),
            )
            await agent.start()
            created.append(
                {
                    "agent_id": agent.id,
                    "name": agent.config.name,
                    "status": agent.status.name,
                }
            )
            audit(
                "agent.spawn",
                user,
                f"agent:{agent.id}",
                "create",
                "success",
                name=agent.config.name,
                model=agent.config.model,
                bulk=True,
            )
        except Exception as e:
            errors.append({"index": idx, "name": cfg.get("name"), "error": str(e)})

    return JSONResponse(
        content={
            "created": created,
            "errors": errors,
            "total_requested": len(agents_config),
            "total_created": len(created),
        }
    )


@router.post("/agents/bulk/stop")
async def agents_bulk_stop(
    body: dict[str, Any],
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Stop multiple agents in one request."""
    runtime = await get_runtime(user)
    agent_ids: list[str] = body.get("agent_ids", [])

    if not agent_ids:
        raise HTTPException(status_code=400, detail="No agent_ids provided")

    stopped: list[str] = []
    errors: list[dict[str, Any]] = []

    for agent_id in agent_ids:
        try:
            agent = runtime.get_agent(agent_id)
            if agent is None:
                errors.append({"agent_id": agent_id, "error": "Agent not found"})
                continue
            await agent.stop()
            stopped.append(agent_id)
            audit("agent.stop", user, f"agent:{agent_id}", "stop", "success", bulk=True)
        except Exception as e:
            errors.append({"agent_id": agent_id, "error": str(e)})

    return JSONResponse(
        content={
            "stopped": stopped,
            "errors": errors,
            "total_requested": len(agent_ids),
            "total_stopped": len(stopped),
        }
    )


@router.post("/agents/bulk/tag")
async def agents_bulk_tag(
    body: dict[str, Any],
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Add tags to multiple agents."""
    runtime = await get_runtime(user)
    agent_ids: list[str] = body.get("agent_ids", [])
    tags: list[str] = body.get("tags", [])

    if not agent_ids:
        raise HTTPException(status_code=400, detail="No agent_ids provided")
    if not tags:
        raise HTTPException(status_code=400, detail="No tags provided")

    tagged: list[str] = []
    errors: list[dict[str, Any]] = []

    for agent_id in agent_ids:
        try:
            agent = runtime.get_agent(agent_id)
            if agent is None:
                errors.append({"agent_id": agent_id, "error": "Agent not found"})
                continue
            current_tags: list[str] = getattr(agent.config, "tags", [])
            new_tags = list(set(current_tags + tags))
            agent.config.tags = new_tags
            tagged.append(agent_id)
        except Exception as e:
            errors.append({"agent_id": agent_id, "error": str(e)})

    return JSONResponse(
        content={
            "tagged": tagged,
            "errors": errors,
            "tags": tags,
        }
    )


# -- templates -------------------------------------------------------------


@router.post("/agent-templates")
async def template_create(
    body: dict[str, Any],
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Create an agent template."""
    template_id = str(uuid.uuid4())
    template: dict[str, Any] = {
        "template_id": template_id,
        "name": body.get("name", "unnamed-template"),
        "model": body.get("model", "claude"),
        "system_prompt": body.get("system_prompt", ""),
        "timeout_seconds": body.get("timeout_seconds", 300),
        "max_iterations": body.get("max_iterations", 10),
        "memory_namespace": body.get("memory_namespace", "default"),
        "tags": body.get("tags", []),
        "created_by": user.user_id,
        "created_at": datetime.now(UTC).isoformat(),
    }

    agent_templates[template_id] = template

    audit(
        "template.create",
        user,
        f"template:{template_id}",
        "create",
        "success",
        name=template["name"],
    )

    return JSONResponse(content=template)


@router.get("/agent-templates")
async def template_list(
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """List all agent templates."""
    templates: list[dict[str, Any]] = list(agent_templates.values())
    return JSONResponse(
        content={
            "templates": templates,
            "count": len(templates),
        }
    )


@router.get("/agent-templates/{template_id}")
async def template_get(
    template_id: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Get a specific agent template."""
    template = agent_templates.get(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail=f"Template {template_id} not found")
    return JSONResponse(content=template)


@router.delete("/agent-templates/{template_id}")
async def template_delete(
    template_id: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Delete an agent template."""
    if template_id not in agent_templates:
        raise HTTPException(status_code=404, detail=f"Template {template_id} not found")

    del agent_templates[template_id]

    audit("template.delete", user, f"template:{template_id}", "delete", "success")

    return JSONResponse(content={"template_id": template_id, "deleted": True})


@router.post("/agents/from-template")
async def agent_spawn_from_template(
    body: dict[str, Any],
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Spawn an agent from a template."""
    runtime = await get_runtime(user)
    template_id: str | None = body.get("template_id")

    if not template_id:
        raise HTTPException(status_code=400, detail="template_id is required")

    template = agent_templates.get(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail=f"Template {template_id} not found")

    agent = runtime.spawn(
        name=body.get("name", f"{template['name']}-instance"),
        model=template.get("model", "claude"),
        system_prompt=template.get("system_prompt", ""),
        memory_namespace=template.get("memory_namespace", "default"),
        max_iterations=template.get("max_iterations", 10),
        tags=template.get("tags", []),
    )

    await agent.start()

    audit(
        "agent.spawn",
        user,
        f"agent:{agent.id}",
        "create",
        "success",
        name=agent.config.name,
        template_id=template_id,
    )

    return JSONResponse(
        content={
            "agent_id": agent.id,
            "name": agent.config.name,
            "status": agent.status.name,
            "template_id": template_id,
            "created_at": agent.created_at.isoformat(),
        }
    )


# -- tags ------------------------------------------------------------------


@router.post("/agents/{agent_id}/tags")
async def agent_add_tags(
    agent_id: str,
    body: dict[str, Any],
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Add tags to an agent."""
    runtime = await get_runtime(user)
    agent = runtime.get_agent(agent_id)

    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    tags: list[str] = body.get("tags", [])
    if not tags:
        raise HTTPException(status_code=400, detail="No tags provided")

    if not hasattr(agent.config, "tags"):
        agent.config.tags = []

    current_tags = set(agent.config.tags)
    new_tags = set(tags)
    agent.config.tags = list(current_tags | new_tags)

    return JSONResponse(
        content={
            "agent_id": agent_id,
            "tags": agent.config.tags,
            "added": list(new_tags - current_tags),
        }
    )


@router.post("/agents/{agent_id}/tags/remove")
async def agent_remove_tags(
    agent_id: str,
    body: dict[str, Any],
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Remove tags from an agent."""
    runtime = await get_runtime(user)
    agent = runtime.get_agent(agent_id)

    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    tags: list[str] = body.get("tags", [])
    if not tags:
        raise HTTPException(status_code=400, detail="No tags provided")

    if not hasattr(agent.config, "tags"):
        return JSONResponse(content={"agent_id": agent_id, "tags": [], "removed": []})

    current_tags = set(agent.config.tags)
    remove_tags = set(tags)
    agent.config.tags = list(current_tags - remove_tags)

    return JSONResponse(
        content={
            "agent_id": agent_id,
            "tags": agent.config.tags,
            "removed": list(remove_tags & current_tags),
        }
    )


@router.get("/agents/{agent_id}/tags")
async def agent_get_tags(
    agent_id: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Get tags for an agent."""
    runtime = await get_runtime(user)
    agent = runtime.get_agent(agent_id)

    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    tags: list[str] = getattr(agent.config, "tags", [])

    return JSONResponse(
        content={
            "agent_id": agent_id,
            "tags": tags,
        }
    )


# -- stream (SSE) ----------------------------------------------------------


@router.post("/agents/{agent_id}/stream")
async def agent_stream(
    agent_id: str,
    body: dict[str, Any],
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> EventSourceResponse:
    """Stream an agent's response as SSE events."""
    runtime = await get_runtime(user)
    agent = runtime.get_agent(agent_id)

    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    prompt: str = body.get("prompt", "")
    context: dict[str, Any] = body.get("context", {})

    async def _event_generator() -> AsyncGenerator[dict[str, str], None]:
        try:
            async for chunk in agent.stream(prompt, **context):
                yield {"event": "chunk", "data": chunk}
            yield {"event": "done", "data": ""}
        except Exception as e:
            yield {"event": "error", "data": str(e)}

    return EventSourceResponse(_event_generator())
