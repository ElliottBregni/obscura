"""Routes: HTTP API for the config wizard.

Mounted at ``/api/v1/wizard`` so an external UI (or curl, or the MCP
shadow tools) can drive the same logic that the TUI calls. Every
handler is a thin wrapper around :class:`obscura.wizard.WizardService`.

Authentication mirrors the rest of the routes — admin role required for
mutations, current-user read access for snapshots. The wizard edits
``~/.obscura/config.toml`` so it is privileged by definition.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from obscura.auth.models import AuthenticatedUser
from obscura.auth.rbac import get_current_user, require_role
from obscura.wizard import (
    Profile,
    WizardSnapshot,
    WizardService,
    WorkspaceBinding,
)

router = APIRouter(prefix="/api/v1/wizard", tags=["wizard"])


def _service() -> WizardService:
    return WizardService()


# ----------------------------------------------------------------------
# Request models — mutations only. Reads use the snapshot directly.
# ----------------------------------------------------------------------


class SetActiveRequest(BaseModel):
    profile: str = Field(..., min_length=1)


class SetWorkspaceRequest(BaseModel):
    path: str = Field(..., min_length=1)
    profile: str = Field(..., min_length=1)


class UnsetWorkspaceRequest(BaseModel):
    path: str = Field(..., min_length=1)


class EnvWriteRequest(BaseModel):
    content: str = Field(default="", description="Full file body; empty clears.")


# ----------------------------------------------------------------------
# Reads
# ----------------------------------------------------------------------


@router.get("/snapshot", response_model=WizardSnapshot)
async def get_snapshot(
    _user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> WizardSnapshot:
    """Full read-only view of profiles, active state, workspace bindings, and discoverables."""
    return _service().snapshot()


@router.get("/profiles", response_model=list[Profile])
async def list_profiles(
    _user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> list[Profile]:
    return _service().list_profiles()


@router.get("/profiles/{name}", response_model=Profile)
async def get_profile(
    name: str,
    _user: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> Profile:
    p = _service().get_profile(name)
    if p is None:
        raise HTTPException(status_code=404, detail=f"profile '{name}' not found")
    return p


# ----------------------------------------------------------------------
# Mutations — admin-gated
# ----------------------------------------------------------------------


@router.put("/profiles/{name}", response_model=Profile)
async def upsert_profile(
    name: str,
    body: Profile,
    _user: Annotated[AuthenticatedUser, Depends(require_role("admin"))],
) -> Profile:
    if body.name != name:
        raise HTTPException(
            status_code=400,
            detail=f"path name '{name}' does not match body name '{body.name}'",
        )
    return _service().upsert_profile(body)


@router.delete("/profiles/{name}")
async def delete_profile(
    name: str,
    _user: Annotated[AuthenticatedUser, Depends(require_role("admin"))],
) -> JSONResponse:
    ok = _service().delete_profile(name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"profile '{name}' not found")
    return JSONResponse(content={"deleted": name})


@router.post("/active")
async def set_active(
    body: SetActiveRequest,
    _user: Annotated[AuthenticatedUser, Depends(require_role("admin"))],
) -> JSONResponse:
    state = _service().set_active(body.profile)
    return JSONResponse(content={"profile": state.profile})


@router.post("/workspaces", response_model=WorkspaceBinding)
async def set_workspace(
    body: SetWorkspaceRequest,
    _user: Annotated[AuthenticatedUser, Depends(require_role("admin"))],
) -> WorkspaceBinding:
    return _service().set_workspace(body.path, body.profile)


@router.delete("/workspaces")
async def unset_workspace(
    body: UnsetWorkspaceRequest,
    _user: Annotated[AuthenticatedUser, Depends(require_role("admin"))],
) -> JSONResponse:
    ok = _service().unset_workspace(body.path)
    if not ok:
        raise HTTPException(status_code=404, detail=f"path '{body.path}' not bound")
    return JSONResponse(content={"unbound": body.path})


# ----------------------------------------------------------------------
# Per-profile env files
# ----------------------------------------------------------------------


@router.get("/env/{profile}")
async def read_env(
    profile: str,
    _user: Annotated[AuthenticatedUser, Depends(require_role("admin"))],
) -> JSONResponse:
    content = _service().read_env_file(profile)
    return JSONResponse(content={"profile": profile, "content": content})


@router.put("/env/{profile}")
async def write_env(
    profile: str,
    body: EnvWriteRequest,
    _user: Annotated[AuthenticatedUser, Depends(require_role("admin"))],
) -> JSONResponse:
    path = _service().write_env_file(profile, body.content)
    return JSONResponse(content={"profile": profile, "path": str(path)})


# ----------------------------------------------------------------------
# SOUL.md (~/.obscura/SOUL.md) — user's personality file
# ----------------------------------------------------------------------


class SoulWriteRequest(BaseModel):
    content: str = Field(default="", description="Full SOUL.md body; empty clears.")


@router.get("/soul")
async def read_soul(
    _user: Annotated[AuthenticatedUser, Depends(require_role("admin"))],
) -> JSONResponse:
    svc = _service()
    return JSONResponse(
        content={"path": str(svc.soul_path()), "content": svc.read_soul()},
    )


@router.put("/soul")
async def write_soul(
    body: SoulWriteRequest,
    _user: Annotated[AuthenticatedUser, Depends(require_role("admin"))],
) -> JSONResponse:
    path = _service().write_soul(body.content)
    return JSONResponse(content={"path": str(path)})
