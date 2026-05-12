"""Typed HTTP clients for the wuzapi sidecar.

Two clients, one per token type:

* :class:`WuzapiClient` — per-user operations (connect, send, status, …).
  Authenticates with the user's token via the ``Token:`` header.
* :class:`WuzapiAdminClient` — admin operations (create/list/delete users).
  Authenticates with the admin token via the ``Authorization:`` header.

Both share an internal envelope unwrapper that validates ``success: true``
and raises a typed :class:`WuzapiAPIError` otherwise. Callers never see
the raw envelope ``{"code", "data", "success"}`` — only typed payloads.

The clients are stateless re: WhatsApp session — they don't track
connection state or cache results. State lives in wuzapi.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Any, Self, TypeVar, cast, override

import httpx
from pydantic import BaseModel, ValidationError

from obscura.integrations.whatsapp.wuzapi.models import (
    WuzapiChatPresenceRequest,
    WuzapiConnectRequest,
    WuzapiConnectResponse,
    WuzapiCreateUserRequest,
    WuzapiEventName,
    WuzapiQRCodeResponse,
    WuzapiSendTextRequest,
    WuzapiSendTextResponse,
    WuzapiSessionStatus,
    WuzapiSetWebhookRequest,
    WuzapiUser,
    WuzapiWebhookConfig,
)

_T = TypeVar("_T", bound=BaseModel)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WuzapiError(Exception):
    """Base for all wuzapi client errors."""


class WuzapiHTTPError(WuzapiError):
    """Non-2xx HTTP response from the sidecar."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"wuzapi HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


class WuzapiAPIError(WuzapiError):
    """2xx response but ``success: false`` in the envelope."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"wuzapi API code={code} message={message!r}")
        self.code = code
        self.message = message


class WuzapiResponseError(WuzapiError):
    """Response shape didn't match the expected Pydantic model."""

    def __init__(self, model: type[BaseModel], body: object) -> None:
        super().__init__(f"wuzapi response does not match {model.__name__}: {body!r}")
        self.model = model
        self.body = body


# ---------------------------------------------------------------------------
# Base client
# ---------------------------------------------------------------------------


class _WuzapiBaseClient(AbstractAsyncContextManager["_WuzapiBaseClient"]):
    """Shared HTTP machinery: envelope unwrapping, lifecycle, base URL.

    Subclasses choose the auth header by overriding :meth:`_auth_headers`.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:18793",
        timeout: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    @override
    async def __aenter__(self) -> Self:
        return self

    @override
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def _auth_headers(self) -> dict[str, str]:
        raise NotImplementedError

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: BaseModel | None = None,
    ) -> object:
        """Make an authenticated request and return the unwrapped ``data`` payload.

        Returns the bare ``data`` value from the wuzapi envelope as ``object``
        (effectively JSON-any). Callers must narrow with ``isinstance(...)``
        before structural access — :meth:`_request_typed` does the right
        thing via Pydantic validation.
        """
        headers = {"Content-Type": "application/json", **self._auth_headers()}
        payload: dict[str, object] | None = (
            json_body.model_dump(by_alias=True, exclude_none=True)
            if json_body is not None
            else None
        )
        resp = await self._http.request(
            method, f"{self._base_url}{path}", headers=headers, json=payload
        )
        if resp.status_code >= 300:
            raise WuzapiHTTPError(resp.status_code, resp.text)
        envelope_raw: object = resp.json()
        if not isinstance(envelope_raw, dict) or "success" not in envelope_raw:
            raise WuzapiResponseError(BaseModel, cast("object", envelope_raw))
        # Cast narrows from post-isinstance `dict[Unknown, Unknown]` to the
        # JSON-shaped envelope. We've validated the outer keys; per-key
        # access is best-effort and runtime-checked.
        envelope: dict[str, Any] = cast("dict[str, Any]", envelope_raw)
        if not envelope["success"]:
            code_val = envelope.get("code", -1)
            code = int(code_val) if isinstance(code_val, int) else -1
            msg = envelope.get("error") or envelope.get("data") or envelope
            raise WuzapiAPIError(code, str(msg))
        return envelope.get("data")

    async def _request_typed(
        self,
        method: str,
        path: str,
        *,
        response_model: type[_T],
        json_body: BaseModel | None = None,
    ) -> _T:
        data = await self._request(method, path, json_body=json_body)
        try:
            return response_model.model_validate(data)
        except ValidationError as exc:
            raise WuzapiResponseError(response_model, data) from exc


# ---------------------------------------------------------------------------
# User-token client (the adapter's workhorse)
# ---------------------------------------------------------------------------


class WuzapiClient(_WuzapiBaseClient):
    """Per-user wuzapi client. Authenticates via the ``Token`` header.

    Typical lifecycle::

        async with WuzapiClient(token="...", base_url="...") as c:
            status = await c.session_status()
            if not status.logged_in:
                qr = await c.get_qr()
                # show QR to user, poll status until logged_in
            await c.send_text(WuzapiSendTextRequest(phone="...", body="..."))
    """

    def __init__(
        self,
        *,
        token: str,
        base_url: str = "http://127.0.0.1:18793",
        timeout: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(base_url=base_url, timeout=timeout, http_client=http_client)
        self._token = token

    @override
    def _auth_headers(self) -> dict[str, str]:
        return {"Token": self._token}

    # ---------- session lifecycle ----------

    async def connect(
        self, req: WuzapiConnectRequest | None = None
    ) -> WuzapiConnectResponse:
        """POST /session/connect — initiate WhatsApp websocket + QR flow."""
        return await self._request_typed(
            "POST", "/session/connect",
            response_model=WuzapiConnectResponse,
            json_body=req or WuzapiConnectRequest(),
        )

    async def disconnect(self) -> None:
        """POST /session/disconnect — close websocket, preserve session."""
        await self._request("POST", "/session/disconnect")

    async def logout(self) -> None:
        """POST /session/logout — full logout, drops linked-device state."""
        await self._request("POST", "/session/logout")

    async def session_status(self) -> WuzapiSessionStatus:
        """GET /session/status — current connection + login state."""
        return await self._request_typed(
            "GET", "/session/status", response_model=WuzapiSessionStatus
        )

    async def get_qr(self) -> WuzapiQRCodeResponse:
        """GET /session/qr — fetch the current QR (empty if already linked)."""
        return await self._request_typed(
            "GET", "/session/qr", response_model=WuzapiQRCodeResponse
        )

    # ---------- messages ----------

    async def send_text(self, req: WuzapiSendTextRequest) -> WuzapiSendTextResponse:
        """POST /chat/send/text — outbound text message."""
        return await self._request_typed(
            "POST", "/chat/send/text",
            response_model=WuzapiSendTextResponse,
            json_body=req,
        )

    async def set_chat_presence(
        self,
        phone: str,
        *,
        state: str,
        media: str = "text",
    ) -> None:
        """POST /chat/presence — set typing/paused indicator in a chat.

        ``state``: ``"composing"`` shows "typing..."; ``"paused"`` clears
        the indicator. WhatsApp times the indicator out after ~10s of
        silence on the presence channel — refresh by re-sending
        ``composing`` if the agent's compose phase runs longer.

        Errors are NOT suppressed here — callers (typically
        ``_TypingTracker``) wrap in try/except so a transient presence
        failure never blocks the actual reply.
        """
        await self._request(
            "POST",
            "/chat/presence",
            json_body=WuzapiChatPresenceRequest(
                phone=phone, state=state, media=media,
            ),
        )

    # ---------- webhook config ----------

    async def set_webhook(
        self,
        url: str,
        *,
        events: list[WuzapiEventName] | None = None,
    ) -> None:
        """POST /webhook — set inbound webhook URL (and optionally events).

        If ``events`` is omitted, wuzapi preserves the existing event filter
        in the DB but **may** desync the in-memory runtime subscription
        (we've seen this happen). When in doubt, pass ``events=["Message"]``
        explicitly so both layers stay in sync.
        """
        await self._request(
            "POST",
            "/webhook",
            json_body=WuzapiSetWebhookRequest(webhook_url=url, events=events),
        )

    async def get_webhook(self) -> WuzapiWebhookConfig:
        """GET /webhook — read current webhook config + subscribed events."""
        return await self._request_typed(
            "GET", "/webhook", response_model=WuzapiWebhookConfig
        )


# ---------------------------------------------------------------------------
# Admin-token client (used during install/setup, not by the adapter)
# ---------------------------------------------------------------------------


class WuzapiAdminClient(_WuzapiBaseClient):
    """Admin operations: list/create/delete wuzapi users.

    Used by the install / bootstrap flow, NOT by the per-message adapter.
    Authenticates via the ``Authorization`` header (the admin token).
    """

    def __init__(
        self,
        *,
        admin_token: str,
        base_url: str = "http://127.0.0.1:18793",
        timeout: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(base_url=base_url, timeout=timeout, http_client=http_client)
        self._admin_token = admin_token

    @override
    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": self._admin_token}

    async def list_users(self) -> list[WuzapiUser]:
        data = await self._request("GET", "/admin/users")
        if not isinstance(data, list):
            raise WuzapiResponseError(WuzapiUser, data)
        users_raw: list[Any] = cast("list[Any]", data)
        return [WuzapiUser.model_validate(u) for u in users_raw]

    async def create_user(self, req: WuzapiCreateUserRequest) -> WuzapiUser:
        return await self._request_typed(
            "POST", "/admin/users",
            response_model=WuzapiUser,
            json_body=req,
        )

    async def delete_user(self, user_id: str) -> None:
        await self._request("DELETE", f"/admin/users/{user_id}")


__all__ = [
    "WuzapiAPIError",
    "WuzapiAdminClient",
    "WuzapiClient",
    "WuzapiError",
    "WuzapiHTTPError",
    "WuzapiResponseError",
]
