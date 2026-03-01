"""Routes: vault sync."""

from __future__ import annotations

import asyncio
import subprocess
import sys

from fastapi import APIRouter, Depends

from obscura.auth.models import AuthenticatedUser
from obscura.auth.rbac import require_role
from obscura.deps import audit, record_sync_metric
from obscura.schemas import SyncRequest, SyncResponse

router = APIRouter(prefix="/api/v1", tags=["sync"])


@router.post("/sync", response_model=SyncResponse)
async def trigger_sync(
    body: SyncRequest,
    user: AuthenticatedUser = Depends(require_role("sync:write")),
) -> SyncResponse:
    """Trigger a vault sync operation."""
    cmd = [sys.executable, "sync.py", "--mode", "symlink"]
    if body.agent:
        cmd.extend(["--agent", body.agent])
    if body.repo:
        cmd.extend(["--repo", body.repo])
    if body.dry_run:
        cmd.append("--dry-run")

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        success = result.returncode == 0
        message = result.stdout.strip() if success else result.stderr.strip()
        status = "success" if success else "error"
        audit(
            "sync.trigger",
            user,
            "sync:vault",
            "execute",
            status,
            agent=body.agent,
            repo=body.repo,
            dry_run=body.dry_run,
        )
        record_sync_metric(status)
        return SyncResponse(success=success, message=message or "sync completed")
    except subprocess.TimeoutExpired:
        audit("sync.trigger", user, "sync:vault", "execute", "error", reason="timeout")
        record_sync_metric("error")
        return SyncResponse(success=False, message="sync timed out after 120s")
    except Exception as exc:
        audit("sync.trigger", user, "sync:vault", "execute", "error", reason=str(exc))
        record_sync_metric("error")
        return SyncResponse(success=False, message=str(exc))
