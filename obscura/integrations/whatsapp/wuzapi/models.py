"""Pydantic models for the wuzapi REST API wire format.

Pure wire-format types — no business logic, no HTTP, no obscura-internal
imports. Mapping to obscura's generic ``PlatformMessage`` happens at the
adapter layer.

Notes on wuzapi quirks captured here:

* Field naming is mixed camelCase / snake_case on the wire. Each model
  declares snake_case attribute names with explicit ``alias=...`` for fields
  whose wire spelling differs.
* ``populate_by_name=True`` lets callers construct models with either name.
* ``extra="ignore"`` — wuzapi adds fields between versions (e.g. ``hmac_key``,
  ``history``). Forward-tolerant by default; switch to ``"forbid"`` per-model
  only if a strict assertion matters.
* The send-text response's ``Timestamp`` is a Unix epoch *integer* on the
  wire despite docs claiming ISO8601. We model it as ``int``.
* The inbound webhook payload's inner ``event`` shape varies by event kind.
  We expose it as a discriminated union (``WuzapiEvent``) where each variant
  parses the relevant subset; unknown event kinds become
  ``WuzapiUnknownEvent`` so callers can log + skip rather than crash.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Closed sets
# ---------------------------------------------------------------------------

WuzapiEventName = Literal["Message", "ReadReceipt", "HistorySync", "ChatPresence"]
"""Event types wuzapi can deliver via webhook (per API.md ## Webhook)."""


# ---------------------------------------------------------------------------
# Inner config blocks (returned inside user objects)
# ---------------------------------------------------------------------------


class WuzapiProxyConfig(BaseModel):
    """Proxy configuration carried inside a user object. Disabled by default."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    enabled: bool = False
    proxy_url: str = ""


class WuzapiS3Config(BaseModel):
    """S3 configuration for media uploads. Disabled in our deployment."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    enabled: bool = False
    bucket: str = ""
    region: str = ""
    endpoint: str = ""
    path_style: bool = False
    public_url: str = ""
    media_delivery: str = ""
    retention_days: int = 0


# ---------------------------------------------------------------------------
# Users / sessions
# ---------------------------------------------------------------------------


class WuzapiUser(BaseModel):
    """A wuzapi user (one WhatsApp account slot).

    Returned by ``GET /admin/users`` and ``POST /admin/users``. Tokens are
    sensitive — never log this object verbatim.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    name: str
    token: str
    webhook: str = ""
    events: str = ""
    expiration: int = 0
    jid: str = ""
    qr_code: str = Field(default="", alias="qrcode")
    connected: bool = False
    logged_in: bool = Field(default=False, alias="loggedIn")
    proxy_config: WuzapiProxyConfig = Field(default_factory=WuzapiProxyConfig)
    s3_config: WuzapiS3Config = Field(default_factory=WuzapiS3Config)

    @property
    def subscribed_events(self) -> tuple[WuzapiEventName, ...]:
        """Parse the comma-separated ``events`` field into a typed tuple."""
        if not self.events:
            return ()
        valid: set[WuzapiEventName] = {
            "Message", "ReadReceipt", "HistorySync", "ChatPresence",
        }
        return tuple(e for e in (p.strip() for p in self.events.split(",")) if e in valid)  # type: ignore[misc]


class WuzapiSessionStatus(BaseModel):
    """Output of ``GET /session/status``.

    Same shape as ``WuzapiUser`` with a few status-specific extras
    (``history``, ``hmac_configured``). The ``loggedIn`` field is the
    canonical "is WhatsApp actually linked" signal; ``connected`` only means
    wuzapi's websocket to WhatsApp servers is open.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    name: str
    jid: str = ""
    events: str = ""
    connected: bool = False
    logged_in: bool = Field(default=False, alias="loggedIn")
    qr_code: str = Field(default="", alias="qrcode")
    history: str = "0"
    hmac_configured: bool = False
    proxy_config: WuzapiProxyConfig = Field(default_factory=WuzapiProxyConfig)
    s3_config: WuzapiS3Config = Field(default_factory=WuzapiS3Config)


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


class WuzapiCreateUserRequest(BaseModel):
    """Body for ``POST /admin/users``."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    token: str
    events: str = "Message"
    webhook: str = ""


class WuzapiConnectRequest(BaseModel):
    """Body for ``POST /session/connect``.

    ``Subscribe`` is wuzapi's per-call event filter (intersected with the
    user's stored ``events``). ``Immediate=True`` makes the call return
    without waiting for login confirmation.
    """

    model_config = ConfigDict(populate_by_name=True)

    subscribe: list[WuzapiEventName] = Field(
        default_factory=lambda: ["Message"], alias="Subscribe"
    )
    immediate: bool = Field(default=True, alias="Immediate")


class WuzapiSendTextContextInfo(BaseModel):
    """Reply context: which message we're replying to."""

    model_config = ConfigDict(populate_by_name=True)

    stanza_id: str = Field(alias="StanzaId")
    participant: str = Field(alias="Participant")


class WuzapiSendTextRequest(BaseModel):
    """Body for ``POST /chat/send/text``.

    ``phone`` is a bare WhatsApp number (no plus, no ``@s.whatsapp.net``).
    Wuzapi normalises it server-side. If ``id`` is omitted wuzapi generates
    one.
    """

    model_config = ConfigDict(populate_by_name=True)

    phone: str = Field(alias="Phone")
    body: str = Field(alias="Body")
    id: str | None = Field(default=None, alias="Id")
    link_preview: bool | None = Field(default=None, alias="LinkPreview")
    context_info: WuzapiSendTextContextInfo | None = Field(
        default=None, alias="ContextInfo"
    )


class WuzapiSetWebhookRequest(BaseModel):
    """Body for ``POST /webhook``.

    Two non-obvious wire-format facts discovered against a running wuzapi:

    * Field name is lowercase ``webhookurl`` (Go's case-insensitive JSON
      decoding lets ``webhookURL`` slip through, but the source-of-truth tag
      is lowercase — alias matches the canonical form).
    * ``events`` is ``[]string`` (a JSON array of strings), NOT a
      comma-separated string. Passing a string returns HTTP 400
      "could not decode payload".

    Also: if ``events`` is omitted or empty, wuzapi only updates the
    webhook URL and **does not** touch the events list. To explicitly clear
    events, we'd need a separate flow; we don't need that path.
    """

    model_config = ConfigDict(populate_by_name=True)

    webhook_url: str = Field(alias="webhookurl")
    events: list[WuzapiEventName] | None = None


# ---------------------------------------------------------------------------
# Responses (the unwrapped ``data`` payload — envelope handled in client)
# ---------------------------------------------------------------------------


class WuzapiConnectResponse(BaseModel):
    """Output of ``POST /session/connect``."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    details: str
    events: str = ""
    jid: str = ""
    webhook: str = ""


class WuzapiSendTextResponse(BaseModel):
    """Output of ``POST /chat/send/text``.

    ``Timestamp`` is a Unix epoch int on the wire even though docs show
    ISO8601 — keep ``int`` and convert in the adapter layer.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    details: str = Field(alias="Details")
    id: str = Field(alias="Id")
    timestamp: int = Field(alias="Timestamp")


class WuzapiQRCodeResponse(BaseModel):
    """Output of ``GET /session/qr``.

    Empty ``qr_code`` means either the session is already linked or wuzapi
    is between QR refreshes — callers should consult ``WuzapiSessionStatus``
    to disambiguate.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    qr_code: str = Field(default="", alias="QRCode")


class WuzapiWebhookConfig(BaseModel):
    """Output of ``GET /webhook``."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    webhook: str
    subscribe: list[WuzapiEventName] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Webhook event payloads (inbound)
# ---------------------------------------------------------------------------
#
# The inbound webhook envelope is ``{"event": {...}, ...optional_media}``.
# The inner ``event`` shape depends on the event kind. We model the variants
# we care about and keep an ``Unknown`` fallback so unknown kinds don't crash
# the receiver.
#
# The actual JSON keys whatsmeow emits are unstable across versions, so each
# variant tolerates unknown extras (``extra="ignore"``) and only requires
# the fields the adapter actually consumes.


class WuzapiMessageEvent(BaseModel):
    """A ``Message`` event — what wuzapi sends when an inbound text arrives.

    The full whatsmeow payload includes many nested fields; we only declare
    the ones the adapter reads. Other fields are ignored. The discriminator
    is ``type == "Message"`` at the envelope level.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    # The wmiau.go dispatcher emits Info{...} + Message{...} sub-structs.
    # We capture them as raw dicts and pluck what we need; once we have
    # confirmed payload samples we can refine these into typed nested
    # models. Marked here as a deliberate TODO.
    info: dict[str, object] = Field(default_factory=dict, alias="Info")
    raw_message: dict[str, object] = Field(default_factory=dict, alias="Message")


class WuzapiUnknownEvent(BaseModel):
    """Catch-all for event kinds the adapter doesn't yet handle.

    Receiver can log + drop rather than crashing.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")


WuzapiEventPayload = Annotated[
    Union[WuzapiMessageEvent, WuzapiUnknownEvent],
    Field(description="Inner event shape, discriminated by envelope.type"),
]


class WuzapiWebhookEnvelope(BaseModel):
    """Top-level shape POSTed by wuzapi to the configured webhook URL.

    Discriminator for parsing: ``type`` field at the envelope level (one of
    the ``WuzapiEventName`` literals). Optional media-delivery fields
    (``base64``, ``s3``, ``mimeType``, ``fileName``) appear when the message
    has an attachment.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    type: str = ""
    event: dict[str, object] = Field(default_factory=dict)
    token: str = ""
    base64: str | None = None
    mime_type: str | None = Field(default=None, alias="mimeType")
    file_name: str | None = Field(default=None, alias="fileName")


__all__ = [
    "WuzapiConnectRequest",
    "WuzapiConnectResponse",
    "WuzapiCreateUserRequest",
    "WuzapiEventName",
    "WuzapiEventPayload",
    "WuzapiMessageEvent",
    "WuzapiProxyConfig",
    "WuzapiQRCodeResponse",
    "WuzapiS3Config",
    "WuzapiSendTextContextInfo",
    "WuzapiSendTextRequest",
    "WuzapiSendTextResponse",
    "WuzapiSessionStatus",
    "WuzapiSetWebhookRequest",
    "WuzapiUnknownEvent",
    "WuzapiUser",
    "WuzapiWebhookConfig",
    "WuzapiWebhookEnvelope",
]
