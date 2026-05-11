"""Inbound webhook receiver for wuzapi events.

wuzapi POSTs JSON envelopes to a URL configured via ``set_webhook``. This
module provides a small Starlette ASGI app that:

1. Accepts ``POST <prefix>`` (default ``/inbound``)
2. Parses the body into :class:`WuzapiWebhookEnvelope`
3. Hands the typed envelope to a caller-supplied async handler
4. Returns ``{"ok": true}`` so wuzapi doesn't retry

The app is intentionally tiny and stateless. Lifecycle is owned by the
caller — bring it up under ``uvicorn.Server`` when ``[messaging.whatsapp]
enabled = true`` and tear it down when the REPL exits. There is no global
state inside this module.

Authentication
--------------
Loopback-only bind (``127.0.0.1``) is the primary defense. If you want
defence-in-depth, configure wuzapi's HMAC signing and supply a
``verify_signature`` callable when constructing the receiver — it'll be
called with raw body bytes + the ``X-Signature`` header before parsing.

Why Starlette and not stdlib ``http.server``: we already import Starlette
(via FastAPI). Async-native, plays nicely with the REPL's event loop.
Adds zero new dependencies.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Final

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from obscura.integrations.whatsapp.wuzapi.models import WuzapiWebhookEnvelope

logger = logging.getLogger(__name__)

WebhookHandler = Callable[[WuzapiWebhookEnvelope], Awaitable[None]]
"""Caller-supplied async handler. Should not raise; log + drop on its end."""

SignatureVerifier = Callable[[bytes, str], bool]
"""Optional. Given (raw_body_bytes, x_signature_header), return True if valid."""


# ---------------------------------------------------------------------------
# Limits + defaults
# ---------------------------------------------------------------------------

_MAX_BODY_BYTES: Final[int] = 1_048_576  # 1 MiB — generous; text messages are <2KB
_DEFAULT_PATH: Final[str] = "/inbound"


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def build_webhook_app(
    *,
    on_event: WebhookHandler,
    path: str = _DEFAULT_PATH,
    verify_signature: SignatureVerifier | None = None,
) -> Starlette:
    """Construct an ASGI app that posts events to ``on_event``.

    The returned ``Starlette`` instance is ready to host inside a
    ``uvicorn.Server`` running on ``127.0.0.1`` at the caller's chosen port.

    ``on_event`` is invoked **without awaiting** completion in the
    request-response cycle — we schedule it as a background task so the
    POST response returns immediately. wuzapi has a 30s retry timer; we
    avoid pressure by acking fast and processing async.

    :param on_event: Async handler for each parsed envelope. Must be safe
        to call concurrently — the receiver doesn't serialise.
    :param path: URL path the receiver listens on. Defaults to ``/inbound``.
    :param verify_signature: Optional HMAC verifier. If provided, requests
        without a valid ``X-Signature`` are rejected with 401.
    """

    async def handle(request: Request) -> JSONResponse:
        body = await request.body()
        if len(body) > _MAX_BODY_BYTES:
            logger.warning("wuzapi webhook body too large (%d bytes), dropping", len(body))
            return JSONResponse({"ok": False, "error": "too_large"}, status_code=413)

        if verify_signature is not None:
            sig = request.headers.get("X-Signature", "")
            if not verify_signature(body, sig):
                logger.warning("wuzapi webhook signature rejected from %s", request.client)
                return JSONResponse({"ok": False, "error": "bad_sig"}, status_code=401)

        try:
            envelope = WuzapiWebhookEnvelope.model_validate_json(body)
        except ValidationError as exc:
            logger.warning("wuzapi webhook payload failed validation: %s", exc)
            # Still 200 so wuzapi doesn't retry; we logged for ourselves.
            return JSONResponse({"ok": True, "ignored": "validation_error"})

        # Fire-and-forget the handler so we ack wuzapi instantly.
        asyncio.create_task(_safe_dispatch(on_event, envelope))
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route(path, handle, methods=["POST"])])
    return app


async def _safe_dispatch(
    handler: WebhookHandler, envelope: WuzapiWebhookEnvelope
) -> None:
    """Run the user's handler with exception isolation."""
    try:
        await handler(envelope)
    except Exception:
        logger.exception(
            "wuzapi webhook handler raised on event type=%r", envelope.type
        )


__all__ = [
    "SignatureVerifier",
    "WebhookHandler",
    "build_webhook_app",
]
