"""Routes: webhook CRUD and delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from sdk.auth.models import AuthenticatedUser
from sdk.auth.rbac import AGENT_READ_ROLES, AGENT_WRITE_ROLES, require_any_role
from sdk.deps import audit

router = APIRouter(prefix="/api/v1", tags=["webhooks"])

# In-memory webhook store
_webhooks: dict[str, dict] = {}


@router.post("/webhooks")
async def webhook_create(
    body: dict,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Create a webhook for events."""
    webhook_id = str(uuid.uuid4())
    secret = secrets.token_urlsafe(32)

    webhook = {
        "webhook_id": webhook_id,
        "url": body.get("url"),
        "events": body.get("events", []),
        "secret": secret,
        "active": True,
        "created_by": user.user_id,
        "created_at": datetime.now(UTC).isoformat(),
    }

    _webhooks[webhook_id] = webhook

    audit("webhook.create", user, f"webhook:{webhook_id}", "create", "success",
          url=webhook["url"], events=webhook["events"])

    return JSONResponse(content={
        "webhook_id": webhook_id,
        "url": webhook["url"],
        "events": webhook["events"],
        "secret": secret,
        "active": webhook["active"],
        "created_at": webhook["created_at"],
    })


@router.get("/webhooks")
async def webhook_list(
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """List all webhooks."""
    webhooks = [
        {k: v for k, v in w.items() if k != "secret"}
        for w in _webhooks.values()
    ]
    return JSONResponse(content={
        "webhooks": webhooks,
        "count": len(webhooks),
    })


@router.get("/webhooks/{webhook_id}")
async def webhook_get(
    webhook_id: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Get a specific webhook."""
    webhook = _webhooks.get(webhook_id)
    if webhook is None:
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id} not found")

    return JSONResponse(content={
        k: v for k, v in webhook.items() if k != "secret"
    })


@router.delete("/webhooks/{webhook_id}")
async def webhook_delete(
    webhook_id: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Delete a webhook."""
    if webhook_id not in _webhooks:
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id} not found")

    del _webhooks[webhook_id]

    audit("webhook.delete", user, f"webhook:{webhook_id}", "delete", "success")

    return JSONResponse(content={"webhook_id": webhook_id, "deleted": True})


@router.post("/webhooks/{webhook_id}/test")
async def webhook_test(
    webhook_id: str,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_WRITE_ROLES)),
) -> JSONResponse:
    """Send a test event to a webhook."""
    import httpx

    webhook = _webhooks.get(webhook_id)
    if webhook is None:
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id} not found")

    payload = {
        "event": "test",
        "timestamp": datetime.now(UTC).isoformat(),
        "data": {"message": "This is a test event"},
    }

    payload_json = json.dumps(payload, separators=(',', ':'))
    signature = hmac.new(
        webhook["secret"].encode(),
        payload_json.encode(),
        hashlib.sha256
    ).hexdigest()

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                webhook["url"],
                json=payload,
                headers={
                    "X-Webhook-Signature": f"sha256={signature}",
                    "X-Webhook-ID": webhook_id,
                    "Content-Type": "application/json",
                },
                timeout=30.0
            )
            return JSONResponse(content={
                "webhook_id": webhook_id,
                "status_code": resp.status_code,
                "success": 200 <= resp.status_code < 300,
            })
    except Exception as e:
        return JSONResponse(content={
            "webhook_id": webhook_id,
            "error": str(e),
            "success": False,
        })


# -- webhook delivery (called from deps.audit) ----------------------------


async def trigger_webhooks(event_type: str, data: dict[str, Any]) -> None:
    """Trigger all webhooks subscribed to an event."""
    import httpx

    payload = {
        "event": event_type,
        "timestamp": datetime.now(UTC).isoformat(),
        "data": data,
    }
    payload_json = json.dumps(payload, separators=(',', ':'))

    for webhook in _webhooks.values():
        if not webhook.get("active", True):
            continue
        if event_type not in webhook.get("events", []):
            continue

        signature = hmac.new(
            webhook["secret"].encode(),
            payload_json.encode(),
            hashlib.sha256
        ).hexdigest()

        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    webhook["url"],
                    json=payload,
                    headers={
                        "X-Webhook-Signature": f"sha256={signature}",
                        "X-Webhook-ID": webhook["webhook_id"],
                        "Content-Type": "application/json",
                    },
                    timeout=30.0
                )
        except Exception:
            pass
