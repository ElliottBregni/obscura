"""Routes: workflow CRUD and execution."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from dataclasses import dataclass
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
@dataclass(frozen=True, slots=True)
class Workflow:
    workflow_id: str
    name: str
    description: str
    steps: list[dict[str, Any]]
    created_by: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "name": self.name,
            "description": self.description,
            "steps": self.steps,
            "created_by": self.created_by,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class WorkflowExecution:
    execution_id: str
    workflow_id: str
    status: str
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    step_results: dict[str, Any]
    started_at: str
    completed_at: str | None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "status": self.status,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "step_results": self.step_results,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }


_workflows: dict[str, Workflow] = {}
_workflow_executions: dict[str, WorkflowExecution] = {}


def get_workflows_store() -> dict[str, Workflow]:
    """Read-only access to workflows store (admin stats/tests)."""
    return _workflows


def get_workflow_executions_store() -> dict[str, WorkflowExecution]:
    """Read-only access to workflow executions store (admin stats/tests)."""
    return _workflow_executions


@router.post("/workflows")
async def workflow_create(
    body: dict[str, Any],
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Create a workflow with steps."""
    workflow_id = str(uuid.uuid4())
    steps_value: list[dict[str, Any]] = list(body.get("steps", []))
    workflow = Workflow(
        workflow_id=workflow_id,
        name=str(body.get("name", "unnamed-workflow")),
        description=str(body.get("description", "")),
        steps=steps_value,
        created_by=user.user_id,
        created_at=datetime.now(UTC).isoformat(),
    )

    _workflows[workflow_id] = workflow

    audit("workflow.create", user, f"workflow:{workflow_id}", "create", "success",
          name=workflow.name)

    return JSONResponse(content=workflow.as_dict())


@router.get("/workflows")
async def workflow_list(
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """List all workflows."""
    workflows: list[dict[str, Any]] = [wf.as_dict() for wf in _workflows.values()]
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
    return JSONResponse(content=workflow.as_dict())


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

    workflow = _workflows.get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")

    inputs: dict[str, Any] = dict(body.get("inputs", {}))
    execution_id = str(uuid.uuid4())

    execution = WorkflowExecution(
        execution_id=execution_id,
        workflow_id=workflow_id,
        status="running",
        inputs=inputs,
        outputs={},
        step_results={},
        started_at=datetime.now(UTC).isoformat(),
        completed_at=None,
        error=None,
    )
    _workflow_executions[execution_id] = execution

    steps: list[dict[str, Any]] = list(workflow.steps)
    step_results: dict[str, Any] = execution.step_results

    for step in steps:
        step_name: str = str(step.get("name"))
        template_id: str | None = step.get("agent_template")

        if template_id and template_id in agent_templates:
            template: dict[str, Any] = agent_templates[template_id]
            agent = runtime.spawn(
                name=f"{workflow.name}-{step_name}",
                model=str(template.get("model", "claude")),
                system_prompt=str(template.get("system_prompt", "")),
            )
        else:
            agent = runtime.spawn(
                name=f"{workflow.name}-{step_name}",
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
            execution = WorkflowExecution(
                execution_id=execution.execution_id,
                workflow_id=execution.workflow_id,
                status="failed",
                inputs=execution.inputs,
                outputs=execution.outputs,
                step_results=execution.step_results,
                started_at=execution.started_at,
                completed_at=datetime.now(UTC).isoformat(),
                error=str(e),
            )
            _workflow_executions[execution_id] = execution
            break
        finally:
            await agent.stop()

    if execution.status == "running":
        outputs: dict[str, Any] = execution.outputs
        completed = datetime.now(UTC).isoformat()
        if steps:
            last_step: str = str(steps[-1].get("name"))
            outputs["result"] = step_results.get(last_step)
        execution = WorkflowExecution(
            execution_id=execution.execution_id,
            workflow_id=execution.workflow_id,
            status="completed",
            inputs=execution.inputs,
            outputs=outputs,
            step_results=execution.step_results,
            started_at=execution.started_at,
            completed_at=completed,
            error=None,
        )
        _workflow_executions[execution_id] = execution

    status_str: str = str(execution.status)

    audit("workflow.execute", user, f"workflow:{workflow_id}", "execute", status_str,
          execution_id=execution_id)

    return JSONResponse(content={
        "execution_id": execution.execution_id,
        "workflow_id": execution.workflow_id,
        "status": execution.status,
        "outputs": execution.outputs,
        "step_results": execution.step_results,
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
        e.as_dict() for e in _workflow_executions.values()
        if e.workflow_id == workflow_id
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
    return JSONResponse(content=execution.as_dict())
