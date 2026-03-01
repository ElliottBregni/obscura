"""Routes: list and resolve pending tool approval requests."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from obscura.approvals import (
    get_tool_approval_request,
    list_tool_approval_requests,
    resolve_tool_approval_request,
)
from obscura.auth.models import AuthenticatedUser
from obscura.auth.rbac import AGENT_READ_ROLES, AGENT_WRITE_ROLES, require_any_role

router = APIRouter(prefix="/api/v1", tags=["tool-approvals"])


@router.get("/tool-approvals")
async def tool_approvals_list(
    status: Literal["all", "pending", "approved", "denied", "expired"] = "pending",
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    approvals = await list_tool_approval_requests(user_id=user.user_id, status=status)
    return JSONResponse(
        content={
            "count": len(approvals),
            "status": status,
            "approvals": [entry.to_dict() for entry in approvals],
        }
    )


@router.get("/tool-approvals/{approval_id}")
async def tool_approvals_get(
    approval_id: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    approval = await get_tool_approval_request(approval_id, user_id=user.user_id)
    if approval is None:
        raise HTTPException(status_code=404, detail=f"Approval {approval_id} not found")
    return JSONResponse(content=approval.to_dict())


@router.post("/tool-approvals/{approval_id}/resolve")
async def tool_approvals_resolve(
    approval_id: str,
    body: dict[str, object],
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    approved_raw = body.get("approved")
    if not isinstance(approved_raw, bool):
        raise HTTPException(status_code=400, detail="approved must be true or false")
    reason_raw = body.get("reason")
    reason = str(reason_raw) if isinstance(reason_raw, str) else None

    approval = await resolve_tool_approval_request(
        approval_id,
        user_id=user.user_id,
        approved=approved_raw,
        reason=reason,
    )
    if approval is None:
        raise HTTPException(status_code=404, detail=f"Approval {approval_id} not found")
    return JSONResponse(content=approval.to_dict())
