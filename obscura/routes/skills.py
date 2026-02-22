"""Stub route for skills — returns empty list until skills are implemented."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1", tags=["skills"])


@router.get("/skills")
async def list_skills() -> dict[str, list[object] | int]:
    return {"skills": [], "count": 0}
