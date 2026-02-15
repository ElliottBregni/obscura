"""Routes: workflow CRUD and execution."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from sdk.auth.models import AuthenticatedUser
from sdk.auth.rbac import AGENT_READ_ROLES, AGENT_WRITE_ROLES, require_any_role
from sdk.deps import audit, get_runtime
from sdk.routes.agents import _agent_templates

router = APIRouter(prefix="/api/v1", tags=["workflows"])

# In-memory stores
_workflows: dict[str, dict] = {}
_workflow_executions: dict[str, dict] = {}


@router.post("/workflows")
async def workflow_create(
    body: dict,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Create a workflow with steps."""
    workflow_id = str(uuid.uuid4())
    workflow = {
        "workflow_id": workflow_id,
        "name": body.get("name", "unnamed-workflow"),
        "description": body.get("description", ""),
        "steps": body.get("steps", []),
        "created_by": user.user_id,
        "created_at": datetime.now(UTC).isoformat(),
    }

    _workflows[workflow_id] = workflow

    audit("workflow.create", user, f"workflow:{workflow_id}", "create", "success",
          name=workflow["name"])

    return JSONResponse(content=workflow)


@router.get("/workflows")
async def workflow_list(
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """List all workflows."""
    workflows = list(_workflows.values())
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
    workflow = _workflows.get(workflow_id)
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
    body: dict,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Execute a workflow with inputs."""
    runtime = await get_runtime(user)

    workflow = _workflows.get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")

    inputs = body.get("inputs", {})
    execution_id = str(uuid.uuid4())

    execution = {
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

    steps = workflow.get("steps", [])

    for step in steps:
        step_name = step.get("name")
        template_id = step.get("agent_template")

        if template_id and template_id in _agent_templates:
            template = _agent_templates[template_id]
            agent = runtime.spawn(
                name=f"{workflow['name']}-{step_name}",
                model=template.get("model", "claude"),
                system_prompt=template.get("system_prompt", ""),
            )
        else:
            agent = runtime.spawn(
                name=f"{workflow['name']}-{step_name}",
                model="claude",
            )

        await agent.start()

        prompt_template = step.get("input", "")
        prompt = prompt_template
        for key, value in inputs.items():
            prompt = prompt.replace(f"{{{{{key}}}}}", str(value))
        for prev_step, result in execution["step_results"].items():
            prompt = prompt.replace(f"{{{{{prev_step}.output}}}}", str(result))

        try:
            result = await agent.run(prompt)
            execution["step_results"][step_name] = result
        except Exception as e:
            execution["status"] = "failed"
            execution["error"] = str(e)
            break
        finally:
            await agent.stop()

    if execution["status"] == "running":
        execution["status"] = "completed"
        if steps:
            last_step = steps[-1].get("name")
            execution["outputs"]["result"] = execution["step_results"].get(last_step)

    execution["completed_at"] = datetime.now(UTC).isoformat()

    audit("workflow.execute", user, f"workflow:{workflow_id}", "execute", execution["status"],
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

    executions = [
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
    execution = _workflow_executions.get(execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail=f"Execution {execution_id} not found")
    return JSONResponse(content=execution)
