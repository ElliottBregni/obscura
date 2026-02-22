"""Routes: agent CRUD, bulk ops, templates, tags, streaming."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any, AsyncGenerator, cast

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from obscura.approvals import (
    create_tool_approval_request,
    wait_for_tool_approval,
)
from obscura.auth.models import AuthenticatedUser
from obscura.auth.rbac import AGENT_READ_ROLES, AGENT_WRITE_ROLES, require_any_role
from obscura.core.types import ToolCallInfo
from obscura.deps import audit, get_runtime
from obscura.core.paths import resolve_obscura_mcp_dir

router = APIRouter(prefix="/api/v1", tags=["agents"])

# Template store (in-memory + optional SQLite)
from obscura.routes import template_store
from obscura.schemas.templates import (
    SpawnFromTemplateRequest,
    TemplateCreateRequest,
    TemplateUpdateRequest,
)
from obscura.schemas.agents import AgentBulkSpawnRequest, AgentSpawnRequest

# Backwards-compatible alias: workflows.py imports this dict directly
agent_templates = template_store.get_all()


def get_agent_templates() -> dict[str, dict[str, Any]]:
    """Read-only access to agent templates (for admin stats/tests)."""
    return template_store.get_all()


def clear_agent_templates() -> None:
    """Clear agent templates (testing helper)."""
    template_store.clear()


def get_agent_templates_view() -> dict[str, dict[str, Any]]:
    """Return a shallow copy for safe read access."""
    return dict(template_store.get_all())


def _resolve_spawn_field(
    body: dict[str, Any],
    builder: dict[str, Any],
    key: str,
    default: Any,
) -> Any:
    if key in body:
        return body.get(key)
    return builder.get(key, default)


def _dump_explicit_top_level(model: BaseModel) -> dict[str, Any]:
    """Dump only explicitly provided top-level fields (except builder)."""
    payload = model.model_dump(exclude_none=True)
    explicit = set(model.model_fields_set)
    for key in list(payload.keys()):
        if key == "builder":
            continue
        if key not in explicit:
            payload.pop(key, None)
    return payload


def _string_key_dict(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    typed = cast(dict[Any, Any], value)
    return {str(k): v for k, v in typed.items()}


def _normalize_spawn_request(body: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    from obscura.agent.agents import MCPConfig

    raw_builder = body.get("builder", {})
    builder: dict[str, Any] = (
        cast(dict[str, Any], raw_builder) if isinstance(raw_builder, dict) else {}
    )

    model_raw = _resolve_spawn_field(body, builder, "model", "copilot")
    model = str(model_raw)
    valid_models = ("copilot", "claude", "localllm", "openai", "moonshot")
    if model not in valid_models:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid model '{model}'. Must be one of: {valid_models}",
        )

    system_prompt_base = str(_resolve_spawn_field(body, builder, "system_prompt", ""))
    skills_raw = builder.get("skills", [])
    skills: list[dict[str, Any]] = (
        cast(list[dict[str, Any]], skills_raw) if isinstance(skills_raw, list) else []
    )
    system_prompt = _compose_system_prompt(
        {
            "system_prompt": system_prompt_base,
            "skills": skills,
        }
    )

    raw_a2a_remote_tools = _resolve_spawn_field(
        body,
        builder,
        "a2a_remote_tools",
        {},
    )
    a2a_remote_tools: dict[str, Any] = (
        cast(dict[str, Any], raw_a2a_remote_tools)
        if isinstance(raw_a2a_remote_tools, dict)
        else {}
    )

    mcp_config_payload = body.get("mcp")
    if not isinstance(mcp_config_payload, dict):
        mcp_config_payload = builder.get("mcp")
    mcp_config: MCPConfig
    mcp_payload_map = _string_key_dict(mcp_config_payload)
    if mcp_payload_map is not None:
        mcp_servers_raw = mcp_payload_map.get("servers", [])
        mcp_servers: list[dict[str, Any]] = (
            cast(list[dict[str, Any]], mcp_servers_raw)
            if isinstance(mcp_servers_raw, list)
            else []
        )
        raw_server_names = mcp_payload_map.get("server_names", [])
        mcp_server_names: list[str] = (
            [str(name) for name in cast(list[Any], raw_server_names)]
            if isinstance(raw_server_names, list)
            else []
        )
        mcp_config = MCPConfig(
            enabled=bool(mcp_payload_map.get("enabled", False)),
            servers=mcp_servers,
            config_path=str(
                mcp_payload_map.get("config_path", str(resolve_obscura_mcp_dir()))
            ),
            server_names=mcp_server_names,
            primary_server_name=str(
                mcp_payload_map.get("primary_server_name", "github")
            ),
            auto_discover=bool(mcp_payload_map.get("auto_discover", True)),
            resolve_env=bool(mcp_payload_map.get("resolve_env", True)),
        )
    else:
        mcp_config = _build_mcp_config(builder)

    tags_value = _resolve_spawn_field(body, builder, "tags", [])
    tags: list[str] = (
        [str(tag) for tag in cast(list[Any], tags_value)]
        if isinstance(tags_value, list)
        else []
    )

    spawn_kwargs: dict[str, Any] = {
        "name": str(_resolve_spawn_field(body, builder, "name", "unnamed")),
        "model": model,
        "system_prompt": system_prompt,
        "memory_namespace": str(
            _resolve_spawn_field(body, builder, "memory_namespace", "default")
        ),
        "max_iterations": int(_resolve_spawn_field(body, builder, "max_iterations", 10)),
        "timeout_seconds": float(
            _resolve_spawn_field(body, builder, "timeout_seconds", 300.0)
        ),
        "enable_system_tools": bool(
            _resolve_spawn_field(body, builder, "enable_system_tools", True)
        ),
        "a2a_remote_tools": a2a_remote_tools,
        "mcp": mcp_config,
        "tags": tags,
    }
    parent_agent_id = _resolve_spawn_field(body, builder, "parent_agent_id", None)
    if parent_agent_id is not None:
        spawn_kwargs["parent_agent_id"] = str(parent_agent_id)
    return spawn_kwargs, mcp_config.enabled


# -- CRUD -----------------------------------------------------------------


@router.post("/agents")
async def agent_spawn(
    body: AgentSpawnRequest,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Spawn a new agent."""
    runtime = await get_runtime(user)
    spawn_kwargs, mcp_enabled = _normalize_spawn_request(
        _dump_explicit_top_level(body)
    )
    agent = runtime.spawn(**spawn_kwargs)

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


@router.get("/agents/{agent_id}/peers")
async def agent_list_peers(
    agent_id: str,
    include_self: bool = False,
    discover_remote: bool = False,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """List local peers and configured A2A remote peers for an agent."""
    runtime = await get_runtime(user)
    agent = runtime.get_agent(agent_id)

    if agent is None:
        raise HTTPException(status_code=404, detail=f"Agent {agent_id} not found")

    catalog = await agent.discover_peers(
        include_self=include_self,
        discover_remote=discover_remote,
    )
    return JSONResponse(
        content={
            "agent_id": agent_id,
            "local": [ref.model_dump(mode="json") for ref in catalog.local],
            "remote": [ref.model_dump(mode="json") for ref in catalog.remote],
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
    mode = str(body.get("mode", "run")).strip().lower()
    if mode not in {"run", "loop"}:
        raise HTTPException(
            status_code=400,
            detail="mode must be either 'run' or 'loop'",
        )
    require_tool_approval = bool(body.get("require_tool_approval", False))
    approval_timeout_raw = body.get("approval_timeout_seconds", 300.0)
    try:
        approval_timeout_seconds = float(approval_timeout_raw)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="approval_timeout_seconds must be a positive number",
        )
    if approval_timeout_seconds <= 0:
        raise HTTPException(
            status_code=400,
            detail="approval_timeout_seconds must be a positive number",
        )
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

    async def _on_confirm_tool_call(tool_call: ToolCallInfo) -> bool:
        approval = await create_tool_approval_request(
            user_id=user.user_id,
            agent_id=agent_id,
            tool_use_id=tool_call.tool_use_id,
            tool_name=tool_call.name,
            tool_input=tool_call.input,
        )
        return await wait_for_tool_approval(
            approval.approval_id,
            user_id=user.user_id,
            timeout_seconds=approval_timeout_seconds,
        )

    try:
        if timeout_seconds is None:
            if mode == "run":
                result = await agent.run(prompt, **context)
            else:
                on_confirm = _on_confirm_tool_call if require_tool_approval else None
                result = await agent.run_loop(
                    prompt,
                    on_confirm=on_confirm,
                    **context,
                )
        else:
            if mode == "run":
                run_task = asyncio.create_task(agent.run(prompt, **context))
            else:
                on_confirm = _on_confirm_tool_call if require_tool_approval else None
                run_task = asyncio.create_task(
                    agent.run_loop(
                        prompt,
                        on_confirm=on_confirm,
                        **context,
                    )
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
    body: AgentBulkSpawnRequest,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Spawn multiple agents in one request."""
    runtime = await get_runtime(user)
    agents_config: list[dict[str, Any]] = [
        _dump_explicit_top_level(item) for item in body.agents
    ]

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
            spawn_kwargs, _mcp_enabled = _normalize_spawn_request(cfg)
            builder_payload = cfg.get("builder", {})
            builder_name_missing = not (
                isinstance(builder_payload, dict) and "name" in builder_payload
            )
            if "name" not in cfg and builder_name_missing:
                spawn_kwargs["name"] = f"bulk-agent-{idx}"
            agent = runtime.spawn(**spawn_kwargs)
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
    body: TemplateCreateRequest,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Create an agent template with full APER profile support."""
    template_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()

    template: dict[str, Any] = {
        "template_id": template_id,
        **body.model_dump(exclude={"persist"}),
        "persist": body.persist,
        "created_by": user.user_id,
        "created_at": now,
        "updated_at": now,
    }

    template_store.put(template_id, template)

    if body.persist:
        template_store.persist_template(template_id, template)

    audit(
        "template.create",
        user,
        f"template:{template_id}",
        "create",
        "success",
        name=body.name,
    )

    return JSONResponse(content=template)


@router.get("/agent-templates")
async def template_list(
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """List all agent templates."""
    templates: list[dict[str, Any]] = list(template_store.get_all().values())
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
    template = template_store.get(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail=f"Template {template_id} not found")
    return JSONResponse(content=template)


@router.put("/agent-templates/{template_id}")
async def template_update(
    template_id: str,
    body: TemplateUpdateRequest,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Partial-update an agent template (only non-null fields are merged)."""
    existing = template_store.get(template_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Template {template_id} not found")

    updates = body.model_dump(exclude_none=True)
    for key, value in updates.items():
        if key == "aper_profile" and isinstance(value, dict):
            existing[key] = value
        elif key == "skills" and isinstance(value, list):
            existing[key] = cast(list[Any], value)
        elif key == "mcp_servers" and isinstance(value, list):
            existing[key] = cast(list[Any], value)
        elif key == "a2a_remote_tools" and isinstance(value, dict):
            existing[key] = value
        else:
            existing[key] = value
    existing["updated_at"] = datetime.now(UTC).isoformat()

    template_store.put(template_id, existing)

    if existing.get("persist", False):
        template_store.persist_template(template_id, existing)

    audit("template.update", user, f"template:{template_id}", "update", "success")

    return JSONResponse(content=existing)


@router.delete("/agent-templates/{template_id}")
async def template_delete(
    template_id: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Delete an agent template."""
    existing = template_store.get(template_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Template {template_id} not found")

    was_persisted = existing.get("persist", False)
    template_store.delete(template_id)

    if was_persisted:
        template_store.delete_persisted(template_id)

    audit("template.delete", user, f"template:{template_id}", "delete", "success")

    return JSONResponse(content={"template_id": template_id, "deleted": True})


def _compose_system_prompt(template: dict[str, Any]) -> str:
    """Build system prompt with skills injected (mirrors AgentBuilder pattern)."""
    base: str = template.get("system_prompt", "")
    skills: list[dict[str, Any]] = template.get("skills", [])
    if not skills:
        return base
    parts: list[str] = [base, "", "## Loaded Skills"]
    for skill in skills:
        parts.append(f"### {skill['name']} (source: {skill.get('source', 'inline')})")
        parts.append(str(skill.get("content", "")).strip())
        parts.append("")
    return "\n".join(parts).strip()


def _build_mcp_config(template: dict[str, Any]) -> Any:
    """Build MCPConfig from template fields."""
    from obscura.agent.agents import MCPConfig

    mcp_servers_raw: list[dict[str, Any]] = template.get("mcp_servers", [])
    explicit_servers: list[dict[str, Any]] = []
    for spec in mcp_servers_raw:
        transport = spec.get("transport", "stdio")
        if transport == "stdio":
            explicit_servers.append({
                "transport": "stdio",
                "command": spec.get("command", ""),
                "args": spec.get("args", []),
                "env": spec.get("env", {}),
            })
        else:
            explicit_servers.append({
                "transport": "sse",
                "url": spec.get("url", ""),
                "env": spec.get("env", {}),
            })

    mcp_enabled = template.get("mcp_auto_discover", False) or bool(explicit_servers)
    return MCPConfig(
        enabled=mcp_enabled,
        servers=explicit_servers,
        config_path=template.get("mcp_config_path", "config/mcp-config.json"),
        server_names=template.get("mcp_server_names", []),
        primary_server_name=template.get("mcp_primary_server_name", "github"),
        auto_discover=template.get("mcp_auto_discover", False),
        resolve_env=template.get("mcp_resolve_env", True),
    )


@router.post("/agents/from-template")
async def agent_spawn_from_template(
    body: SpawnFromTemplateRequest,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Spawn an agent from a template with full APER / MCP / A2A support."""
    runtime = await get_runtime(user)

    template = template_store.get(body.template_id)
    if template is None:
        raise HTTPException(
            status_code=404, detail=f"Template {body.template_id} not found"
        )

    system_prompt = _compose_system_prompt(template)
    mcp_config = _build_mcp_config(template)

    # Build a2a_remote_tools dict
    a2a_spec = template.get("a2a_remote_tools")
    a2a_remote_tools: dict[str, Any] = {}
    if a2a_spec is not None:
        a2a_remote_tools = {
            "enabled": a2a_spec.get("enabled", False),
            "urls": a2a_spec.get("urls", []),
        }
        if a2a_spec.get("auth_token") is not None:
            a2a_remote_tools["auth_token"] = a2a_spec["auth_token"]

    agent_name = body.name or f"{template['name']}-instance"

    agent = runtime.spawn(
        name=agent_name,
        model=template.get("model", "claude"),
        system_prompt=system_prompt,
        memory_namespace=template.get("memory_namespace", "default"),
        max_iterations=template.get("max_iterations", 10),
        timeout_seconds=template.get("timeout_seconds", 300.0),
        enable_system_tools=template.get("enable_system_tools", True),
        tags=template.get("tags", []),
        mcp=mcp_config,
        a2a_remote_tools=a2a_remote_tools,
        parent_agent_id=template.get("parent_agent_id"),
    )

    await agent.start()

    # If APER mode and profile present, run the APER loop immediately
    aper_profile_data = template.get("aper_profile")
    aper_result: str | None = None
    if body.mode == "aper" and aper_profile_data is not None and body.prompt:
        from obscura.agent.aper import APERProfile, ServerAPERAgent

        profile = APERProfile(
            analyze_template=aper_profile_data.get(
                "analyze_template",
                "Analyze the user goal and extract constraints.",
            ),
            plan_template=aper_profile_data.get(
                "plan_template",
                "Create a step-by-step plan to solve the goal.",
            ),
            execute_template=aper_profile_data.get(
                "execute_template",
                (
                    "Goal:\n{goal}\n\nAnalysis:\n{analysis}\n\nPlan:\n{plan}\n\n"
                    "Execute using tools where useful and return concise output."
                ),
            ),
            respond_template=aper_profile_data.get(
                "respond_template",
                "Return a final concise answer based on execution output.",
            ),
            max_turns=aper_profile_data.get("max_turns", 8),
        )
        if agent.client is None:
            raise HTTPException(
                status_code=500,
                detail="Agent client was not initialized before APER execution",
            )
        aper_agent = ServerAPERAgent(agent.client, profile=profile, name=agent_name)
        result = await aper_agent.run(body.prompt)
        aper_result = str(result)

    audit(
        "agent.spawn",
        user,
        f"agent:{agent.id}",
        "create",
        "success",
        name=agent.config.name,
        template_id=body.template_id,
        mode=body.mode,
    )

    response: dict[str, Any] = {
        "agent_id": agent.id,
        "name": agent.config.name,
        "status": agent.status.name,
        "template_id": body.template_id,
        "mode": body.mode,
        "aper_enabled": aper_profile_data is not None,
        "created_at": agent.created_at.isoformat(),
    }
    if aper_result is not None:
        response["aper_result"] = aper_result

    return JSONResponse(content=response)


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
