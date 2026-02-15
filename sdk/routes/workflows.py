"""Routes: workflow CRUD and execution."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from sdk.auth.models import AuthenticatedUser
from sdk.auth.rbac import AGENT_READ_ROLES, AGENT_WRITE_ROLES, require_any_role
from sdk.deps import audit, get_runtime
from sdk.routes.agents import _agent_templates as _imported_agent_templates  # pyright: ignore[reportPrivateUsage]

router = APIRouter(prefix="/api/v1", tags=["workflows"])

# Re-export with proper typing so the rest of the file is clean.
agent_templates: dict[str, dict[str, Any]] = _imported_agent_templates

# In-memory stores
_workflows: dict[str, dict[str, Any]] = {}
_workflow_executions: dict[str, dict[str, Any]] = {}


@router.post("/workflows")
async def workflow_create(
    body: dict[str, Any],
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Create a workflow with steps."""
    workflow_id = str(uuid.uuid4())
    steps_value: Any = body.get("steps", [])
    workflow: dict[str, Any] = {
        "workflow_id": workflow_id,
        "name": body.get("name", "unnamed-workflow"),
        "description": body.get("description", ""),
        "steps": steps_value,
        "created_by": user.user_id,
        "created_at": datetime.now(UTC).isoformat(),
    }

    _workflows[workflow_id] = workflow

    audit("workflow.create", user, f"workflow:{workflow_id}", "create", "success",
          name=str(workflow["name"]))

    return JSONResponse(content=workflow)


@router.get("/workflows")
async def workflow_list(
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """List all workflows."""
    workflows: list[dict[str, Any]] = list(_workflows.values())
    return JSONResponse(content={
        "workflows": workflows,
        "count": len(workflows),
    })


@router.get("/workflows/{workflow_id}")
async def workflow_get(
    workflow_id: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Get a specific workflow."""
    workflow: dict[str, Any] | None = _workflows.get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")
    return JSONResponse(content=workflow)


@router.delete("/workflows/{workflow_id}")
async def workflow_delete(
    workflow_id: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Delete a workflow."""
    if workflow_id not in _workflows:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")

    del _workflows[workflow_id]

    audit("workflow.delete", user, f"workflow:{workflow_id}", "delete", "success")

    return JSONResponse(content={"workflow_id": workflow_id, "deleted": True})


@router.post("/workflows/{workflow_id}/execute")
async def workflow_execute(
    workflow_id: str,
    body: dict[str, Any],
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Execute a workflow with inputs."""
    runtime = await get_runtime(user)

    workflow: dict[str, Any] | None = _workflows.get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")

    inputs: dict[str, Any] = dict(body.get("inputs", {}))
    execution_id = str(uuid.uuid4())

    execution: dict[str, Any] = {
        "execution_id": execution_id,
        "workflow_id": workflow_id,
        "status": "running",
        "inputs": inputs,
        "outputs": {},
        "step_results": {},
        "started_at": datetime.now(UTC).isoformat(),
        "completed_at": None,
    }
    _workflow_executions[execution_id] = execution

    steps: list[dict[str, Any]] = list(workflow.get("steps", []))
    step_results: dict[str, Any] = execution["step_results"]

    for step in steps:
        step_name: str = str(step.get("name"))
        template_id: str | None = step.get("agent_template")

        if template_id and template_id in agent_templates:
            template: dict[str, Any] = agent_templates[template_id]
            agent = runtime.spawn(
                name=f"{workflow['name']}-{step_name}",
                model=str(template.get("model", "claude")),
                system_prompt=str(template.get("system_prompt", "")),
            )
        else:
            agent = runtime.spawn(
                name=f"{workflow['name']}-{step_name}",
                model="claude",
            )

        await agent.start()

        prompt_template: str = str(step.get("input", ""))
        prompt: str = prompt_template
        for key, value in inputs.items():
            prompt = prompt.replace(f"{{{{{key}}}}}", str(value))
        for prev_step_name, prev_result in step_results.items():
            prompt = prompt.replace(f"{{{{{prev_step_name}.output}}}}", str(prev_result))

        try:
            result: Any = await agent.run(prompt)
            step_results[step_name] = result
        except Exception as e:
            execution["status"] = "failed"
            execution["error"] = str(e)
            break
        finally:
            await agent.stop()

    if execution["status"] == "running":
        execution["status"] = "completed"
        if steps:
            last_step: str = str(steps[-1].get("name"))
            outputs: dict[str, Any] = execution["outputs"]
            outputs["result"] = step_results.get(last_step)

    execution["completed_at"] = datetime.now(UTC).isoformat()

    status_str: str = str(execution["status"])

    audit("workflow.execute", user, f"workflow:{workflow_id}", "execute", status_str,
          execution_id=execution_id)

    return JSONResponse(content={
        "execution_id": execution_id,
        "workflow_id": workflow_id,
        "status": execution["status"],
        "outputs": execution["outputs"],
        "step_results": execution["step_results"],
    })


@router.get("/workflows/{workflow_id}/executions")
async def workflow_list_executions(
    workflow_id: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """List executions for a workflow."""
    if workflow_id not in _workflows:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")

    executions: list[dict[str, Any]] = [
        e for e in _workflow_executions.values()
        if e["workflow_id"] == workflow_id
    ]

    return JSONResponse(content={
        "workflow_id": workflow_id,
        "executions": executions,
        "count": len(executions),
    })


@router.get("/workflows/executions/{execution_id}")
async def workflow_get_execution(
    execution_id: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Get a specific execution."""
    execution: dict[str, Any] | None = _workflow_executions.get(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")
    return JSONResponse(content=execution)
