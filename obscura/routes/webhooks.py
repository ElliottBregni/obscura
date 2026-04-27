"""Routes: webhook CRUD and delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from obscura.auth.rbac import AGENT_READ_ROLES, AGENT_WRITE_ROLES, require_any_role
from obscura.deps import audit
from obscura.tools.system import validate_url

from obscura.auth.models import AuthenticatedUser

router = APIRouter(prefix="/api/v1", tags=["webhooks"])


def _validate_webhook_url(url: str) -> str:
    """SSRF-validate a webhook destination URL (SOC2 finding E2).

    Wraps :func:`obscura.tools.system.validate_url` so private/loopback/
    cloud-metadata addresses (169.254.169.254, RFC1918, ::1) are rejected
    before any HTTP request leaves the box, raising
    :class:`fastapi.HTTPException` with 422 on failure.
    """
    if not url:
        raise HTTPException(status_code=422, detail="webhook url is required")
    try:
        return validate_url(url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"webhook url rejected: {exc}")


# In-memory webhook store
@dataclass(frozen=True, slots=True)
class WebhookConfig:
    webhook_id: str
    url: str
    events: list[str]
    secret: str
    active: bool
    created_by: str
    created_at: str

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "webhook_id": self.webhook_id,
            "url": self.url,
            "events": self.events,
            "active": self.active,
            "created_by": self.created_by,
            "created_at": self.created_at,
        }


_webhooks: dict[str, WebhookConfig] = {}


def get_webhooks_store() -> dict[str, WebhookConfig]:
    """Read-only access to webhook store (admin stats/tests)."""
    return _webhooks


@router.post("/webhooks")
async def webhook_create(
    body: dict[str, Any],
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_WRITE_ROLES))],
) -> JSONResponse:
    """Create a webhook for events."""
    webhook_id = str(uuid.uuid4())
    secret = secrets.token_urlsafe(32)
    url = _validate_webhook_url(str(body.get("url", "")))

    webhook = WebhookConfig(
        webhook_id=webhook_id,
        url=url,
        events=list(body.get("events", [])),
        secret=secret,
        active=True,
        created_by=user.user_id,
        created_at=datetime.now(UTC).isoformat(),
    )

    _webhooks[webhook_id] = webhook

    audit(
        "webhook.create",
        user,
        f"webhook:{webhook_id}",
        "create",
        "success",
        url=webhook.url,
        events=webhook.events,
    )

    return JSONResponse(
        content={
            **webhook.as_public_dict(),
            "secret": secret,
        },
    )


@router.get("/webhooks")
async def webhook_list(
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
) -> JSONResponse:
    """List all webhooks."""
    webhooks: list[dict[str, Any]] = [w.as_public_dict() for w in _webhooks.values()]
    return JSONResponse(
        content={
            "webhooks": webhooks,
            "count": len(webhooks),
        },
    )


@router.get("/webhooks/{webhook_id}")
async def webhook_get(
    webhook_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
) -> JSONResponse:
    """Get a specific webhook."""
    webhook = _webhooks.get(webhook_id)
    if webhook is None:
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id} not found")

    return JSONResponse(content=webhook.as_public_dict())


@router.delete("/webhooks/{webhook_id}")
async def webhook_delete(
    webhook_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_WRITE_ROLES))],
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
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_WRITE_ROLES))],
) -> JSONResponse:
    """Send a test event to a webhook."""
    import httpx

    webhook = _webhooks.get(webhook_id)
    if webhook is None:
        raise HTTPException(status_code=404, detail=f"Webhook {webhook_id} not found")

    payload: dict[str, Any] = {
        "event": "test",
        "timestamp": datetime.now(UTC).isoformat(),
        "data": {"message": "This is a test event"},
    }

    payload_json = json.dumps(payload, separators=(",", ":"))
    webhook_secret: str = webhook.secret
    signature = hmac.new(
        webhook_secret.encode(),
        payload_json.encode(),
        hashlib.sha256,
    ).hexdigest()

    try:
        webhook_url: str = _validate_webhook_url(webhook.url)
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "webhook_id": webhook_id,
                "error": exc.detail,
                "success": False,
            },
        )

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                webhook_url,
                json=payload,
                headers={
                    "X-Webhook-Signature": f"sha256={signature}",
                    "X-Webhook-ID": webhook_id,
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
            return JSONResponse(
                content={
                    "webhook_id": webhook_id,
                    "status_code": resp.status_code,
                    "success": 200 <= resp.status_code < 300,
                },
            )
    except Exception as e:
        return JSONResponse(
            content={
                "webhook_id": webhook_id,
                "error": str(e),
                "success": False,
            },
        )


# -- webhook delivery (called from deps.audit) ----------------------------


async def trigger_webhooks(event_type: str, data: dict[str, Any]) -> None:
    """Trigger all webhooks subscribed to an event."""
    import httpx

    payload: dict[str, Any] = {
        "event": event_type,
        "timestamp": datetime.now(UTC).isoformat(),
        "data": data,
    }
    payload_json = json.dumps(payload, separators=(",", ":"))

    for webhook in _webhooks.values():
        # Webhooks stored as dicts in tests; accept both dict and object forms
        if isinstance(webhook, dict):
            wdict = cast("dict[str, Any]", webhook)
            active: bool = bool(wdict.get("active"))
            events_raw = cast("list[Any]", wdict.get("events", []))
            events: list[str] = [str(e) for e in events_raw]
            webhook_secret: str = str(wdict.get("secret", ""))
            webhook_id: str = str(wdict.get("webhook_id", ""))
            webhook_url: str = str(wdict.get("url", ""))
        else:
            active = webhook.active
            events = webhook.events
            webhook_secret = webhook.secret
            webhook_id = webhook.webhook_id
            webhook_url = webhook.url

        if not active:
            continue
        if event_type not in events:
            continue

        try:
            validated_url = validate_url(webhook_url)
        except ValueError:
            # Stored URL no longer passes the SSRF guard (DNS rebound,
            # allowlist tightened, etc.) — skip silently rather than make
            # the request.
            continue

        signature = hmac.new(
            webhook_secret.encode(),
            payload_json.encode(),
            hashlib.sha256,
        ).hexdigest()

        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    validated_url,
                    json=payload,
                    headers={
                        "X-Webhook-Signature": f"sha256={signature}",
                        "X-Webhook-ID": webhook_id,
                        "Content-Type": "application/json",
                    },
                    timeout=30.0,
                )
        except Exception:
            pass
