"""Channel webhook routes — Telegram and WhatsApp inbound message handlers.

Mount this router on the existing Obscura FastAPI app:

    from obscura.routes.channels import router as channels_router
    app.include_router(channels_router)

Endpoints:
    POST /channels/telegram/webhook         — Telegram Bot webhook
    GET  /channels/whatsapp/verify          — Meta webhook verification challenge
    POST /channels/whatsapp/webhook         — WhatsApp Cloud API inbound messages

    POST   /channels/configs                — Register a new channel config (spec-driven)
    GET    /channels/configs                — List all channel configs
    GET    /channels/configs/{config_id}    — Get one channel config
    PATCH  /channels/configs/{config_id}    — Update a channel config
    DELETE /channels/configs/{config_id}    — Delete a channel config
    POST   /channels/configs/{config_id}/apply — Hot-reload: apply config to live router
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from obscura.auth.models import AuthenticatedUser
from obscura.auth.rbac import AGENT_READ_ROLES, AGENT_WRITE_ROLES, require_any_role
from obscura.integrations.messaging.store import ChannelConfigStore
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/channels", tags=["channels"])

# Module-level router instance — set at startup via init_channel_router()
_channel_router: Any = None


def init_channel_router(channel_router: Any) -> None:
    """Call this at app startup to wire up the ChannelRouter instance."""
    global _channel_router  # noqa: PLW0603
    _channel_router = channel_router
    logger.info("Channel webhook routes initialized")


def _get_router() -> Any:
    if _channel_router is None:
        raise HTTPException(status_code=503, detail="Channel router not initialized")
    return _channel_router


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, str]:
    """Receive a Telegram Bot API update via webhook."""
    channel_router = _get_router()

    # Verify secret token if configured
    telegram_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    strict: bool = getattr(request.app.state, "strict_webhook_verification", False)
    if not telegram_secret and strict:
        raise HTTPException(status_code=503, detail="Telegram webhook secret not configured")
    if telegram_secret:
        if not x_telegram_bot_api_secret_token:
            logger.warning("Telegram webhook: missing secret token header")
            raise HTTPException(status_code=403, detail="Missing secret token")
        if not hmac.compare_digest(telegram_secret, x_telegram_bot_api_secret_token):
            logger.warning("Telegram webhook: invalid secret token")
            raise HTTPException(status_code=403, detail="Invalid secret token")

    try:
        update: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        # Non-message update (e.g. bot added to group) — ack and ignore
        return {"status": "ignored"}

    text = msg.get("text") or msg.get("caption", "")
    chat = msg.get("chat", {})
    chat_id = str(chat.get("id", ""))
    sender = msg.get("from", {})
    sender_id = str(sender.get("id", ""))

    if not text or not chat_id:
        return {"status": "ignored"}

    update_id = str(update.get("update_id", ""))
    message_id = hashlib.sha1(f"tg:{update_id}:{chat_id}".encode()).hexdigest()

    import asyncio

    asyncio.create_task(
        channel_router.dispatch(
            platform="telegram",
            sender_id=sender_id,
            text=text,
            channel_id=f"chat:{chat_id}",
            message_id=message_id,
            metadata={"chat_id": chat_id, "update_id": update_id},
        )
    )

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# WhatsApp (Meta Cloud API)
# ---------------------------------------------------------------------------


@router.get("/whatsapp/verify")
async def whatsapp_verify(
    request: Request,
) -> PlainTextResponse:
    """Handle Meta webhook verification challenge (hub.challenge handshake)."""
    params = dict(request.query_params)
    mode = params.get("hub.mode", "")
    verify_token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")

    expected_token = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
    if not expected_token:
        raise HTTPException(
            status_code=500, detail="WHATSAPP_VERIFY_TOKEN not configured"
        )

    if mode == "subscribe" and hmac.compare_digest(verify_token, expected_token):
        logger.info("WhatsApp webhook verified successfully")
        return PlainTextResponse(challenge)

    logger.warning("WhatsApp webhook verification failed: mode=%s", mode)
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/whatsapp/webhook")
async def whatsapp_webhook(
    request: Request,
) -> dict[str, str]:
    """Receive a WhatsApp Cloud API inbound message."""
    channel_router = _get_router()

    body = await request.body()

    # Verify Meta signature
    app_secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    strict: bool = getattr(request.app.state, "strict_webhook_verification", False)
    if not app_secret and strict:
        raise HTTPException(status_code=503, detail="WhatsApp app secret not configured")
    if app_secret:
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_meta_signature(body, sig_header, app_secret):
            logger.warning("WhatsApp webhook: signature verification failed")
            raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        payload: dict[str, Any] = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    import asyncio

    tasks: list[asyncio.Task[Any]] = []

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                msg_type = msg.get("type", "")
                if msg_type == "text":
                    text = msg.get("text", {}).get("body", "")
                elif msg_type in ("image", "audio", "video", "document"):
                    text = f"[{msg_type} attachment]"
                else:
                    continue

                if not text:
                    continue

                from_number = msg.get("from", "")
                message_id = msg.get("id", "")
                phone_number_id = value.get("metadata", {}).get("phone_number_id", "")

                tasks.append(
                    asyncio.create_task(
                        channel_router.dispatch(
                            platform="whatsapp",
                            sender_id=from_number,
                            text=text,
                            channel_id=f"wa:{phone_number_id}",
                            message_id=message_id,
                            metadata={
                                "from_number": from_number,
                                "phone_number_id": phone_number_id,
                                "message_type": msg_type,
                            },
                        )
                    )
                )

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redact_credentials(record_dict: dict) -> dict:
    """Return a copy of *record_dict* with credential values masked.

    Each value in the ``credentials`` sub-dict is replaced with
    ``"<redacted>"`` so keys are visible (useful for debugging) but
    secrets are never returned over the API.
    """
    import copy
    out = copy.deepcopy(record_dict)
    creds = out.get("credentials")
    if isinstance(creds, dict):
        out["credentials"] = {k: "<redacted>" for k in creds}
    return out


def _verify_meta_signature(body: bytes, sig_header: str, app_secret: str) -> bool:
    """Verify X-Hub-Signature-256 from Meta webhook."""
    if not sig_header.startswith("sha256="):
        return False
    mac = hmac.HMAC(app_secret.encode("utf-8"), body, hashlib.sha256)
    expected = "sha256=" + mac.hexdigest()
    return hmac.compare_digest(expected, sig_header)


# ---------------------------------------------------------------------------
# Spec-driven channel configuration — CRUD + hot-reload
# ---------------------------------------------------------------------------

# Module-level singleton for the config store (lazy-initialised)
_config_store: Any = None


def _get_config_store() -> Any:
    global _config_store  # noqa: PLW0603
    if _config_store is None:
        _config_store = ChannelConfigStore()
    return _config_store


# ------------------------------------------------------------------
# Pydantic request / response models
# ------------------------------------------------------------------


class ChannelConfigCreate(BaseModel):
    """Body for POST /channels/configs."""

    platform: str = Field(..., description="'telegram' or 'whatsapp'")
    label: str = Field(default="", description="Human-readable label")
    enabled: bool = Field(default=True)
    credentials: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Platform credentials. "
            "Telegram: {'bot_token': '...', 'webhook_secret': '...'}. "
            "WhatsApp: {'account_sid': '...', 'auth_token': '...', 'from_number': '...'}"
        ),
    )
    router_config: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Optional ChannelRouterConfig overrides: "
            "system_prompt, max_turns, session_timeout_seconds, etc."
        ),
    )
    contacts: list[str] = Field(
        default_factory=list,
        description="Allowlist of user IDs / phone numbers. Empty = allow all.",
    )
    mode: str = Field(
        default="chat",
        description="Execution mode: 'chat' (standard AgentLoop) or 'kairos' (long-horizon goal runtime).",
    )


class ChannelConfigPatch(BaseModel):
    """Body for PATCH /channels/configs/{config_id}."""

    label: str | None = None
    enabled: bool | None = None
    mode: str | None = Field(
        default=None,
        description="Change execution mode: 'chat' or 'kairos'.",
    )
    credentials: dict[str, Any] | None = None
    router_config: dict[str, Any] | None = None
    contacts: list[str] | None = None


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------


@router.post("/configs", status_code=201)
async def create_channel_config(
    body: ChannelConfigCreate,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_WRITE_ROLES))],
) -> dict[str, Any]:
    """Register a new channel configuration."""
    store = _get_config_store()
    record = store.create(
        platform=body.platform,
        label=body.label,
        enabled=body.enabled,
        mode=body.mode,
        credentials=body.credentials,
        router_config=body.router_config,
        contacts=body.contacts,
    )
    return record.to_dict()


@router.get("/configs")
async def list_channel_configs(
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
    enabled_only: bool = False,
) -> list[dict[str, Any]]:
    """List all channel configurations."""
    store = _get_config_store()
    return [_redact_credentials(r.to_dict()) for r in store.list_all(enabled_only=enabled_only)]


@router.get("/configs/{config_id}")
async def get_channel_config(
    config_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_READ_ROLES))],
) -> dict[str, Any]:
    """Get a single channel configuration by ID."""
    store = _get_config_store()
    record = store.get(config_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Config '{config_id}' not found")
    return _redact_credentials(record.to_dict())


@router.patch("/configs/{config_id}")
async def update_channel_config(
    config_id: str,
    body: ChannelConfigPatch,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_WRITE_ROLES))],
) -> dict[str, Any]:
    """Update a channel configuration (partial update)."""
    store = _get_config_store()
    try:
        record = store.update(
            config_id,
            label=body.label,
            enabled=body.enabled,
            mode=body.mode,
            credentials=body.credentials,
            router_config=body.router_config,
            contacts=body.contacts,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Config '{config_id}' not found")
    return record.to_dict()


@router.delete("/configs/{config_id}", status_code=204)
async def delete_channel_config(
    config_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_WRITE_ROLES))],
) -> None:
    """Delete a channel configuration."""
    store = _get_config_store()
    removed = store.delete(config_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Config '{config_id}' not found")


@router.post("/configs/{config_id}/apply")
async def apply_channel_config(
    config_id: str,
    user: Annotated[AuthenticatedUser, Depends(require_any_role(*AGENT_WRITE_ROLES))],
) -> dict[str, str]:
    """Hot-reload: apply a saved config to the live ChannelRouter immediately.

    Builds and registers (or deregisters if disabled) the platform adapter
    without requiring a server restart.
    """
    store = _get_config_store()
    record = store.get(config_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Config '{config_id}' not found")

    channel_router = _get_router()
    try:
        await channel_router.apply_config(record)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        logger.exception("apply_channel_config failed for config_id=%s", config_id)
        raise HTTPException(status_code=500, detail=f"Adapter init failed: {exc}")

    action = "registered" if record.enabled else "deregistered"
    return {
        "status": "ok",
        "config_id": config_id,
        "platform": record.platform,
        "action": action,
    }
