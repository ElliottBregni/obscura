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

from typing import Any, Literal

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
        valid: set[str] = {"Message", "ReadReceipt", "HistorySync", "ChatPresence"}
        names: list[WuzapiEventName] = []
        for part in self.events.split(","):
            stripped = part.strip()
            if stripped in valid:
                # Cast is safe because we just checked membership in the
                # closed set of literal values.
                names.append(stripped)  # type: ignore[arg-type]
        return tuple(names)


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

    Field names match wuzapi's wire shape (``Subscribe``, ``Immediate``)
    via Go's case-insensitive JSON decode — we just spell them Pythonically.
    ``subscribe`` is wuzapi's per-call event filter; ``immediate=True``
    makes the call return without waiting for login confirmation.
    """

    model_config = ConfigDict(populate_by_name=True)

    subscribe: list[WuzapiEventName] = Field(default_factory=lambda: ["Message"])
    immediate: bool = True


class WuzapiSendTextContextInfo(BaseModel):
    """Reply context: which message we're replying to."""

    model_config = ConfigDict(populate_by_name=True)

    stanza_id: str
    participant: str


class WuzapiSendTextRequest(BaseModel):
    """Body for ``POST /chat/send/text``.

    ``phone`` accepts a bare phone number (no plus, no ``@s.whatsapp.net``)
    OR a full WhatsApp JID for groups (``...@g.us``). Wuzapi's parseJID
    server-side routes both correctly.
    """

    model_config = ConfigDict(populate_by_name=True)

    phone: str
    body: str
    id: str | None = None
    link_preview: bool | None = None
    context_info: WuzapiSendTextContextInfo | None = None


class WuzapiDownloadMediaRequest(BaseModel):
    """Body for any of ``/chat/downloadimage``, ``downloadvideo``,
    ``downloaddocument``, ``downloadaudio``.

    All four endpoints take the same encrypted-media metadata from
    the inbound Message webhook (URL, DirectPath, MediaKey, Mimetype,
    FileEncSHA256, FileSHA256, FileLength) and return the decrypted
    bytes wrapped in a data-URL. We share one model and let the
    client method pick the endpoint based on media kind.

    Wire format note: wuzapi's Go struct uses PascalCase field names
    (``Url``, ``DirectPath``, etc.); Go's case-insensitive JSON decode
    matches our Pythonic ``snake_case`` aliases via serialization_alias.
    The SHA hashes and media key arrive as base64-encoded strings on
    the inbound webhook and must be passed through verbatim.
    """

    model_config = ConfigDict(populate_by_name=True)

    url: str = Field(serialization_alias="Url")
    direct_path: str = Field(default="", serialization_alias="DirectPath")
    media_key: str = Field(default="", serialization_alias="MediaKey")
    mimetype: str = Field(default="", serialization_alias="Mimetype")
    file_enc_sha256: str = Field(default="", serialization_alias="FileEncSHA256")
    file_sha256: str = Field(default="", serialization_alias="FileSHA256")
    file_length: int = Field(default=0, serialization_alias="FileLength")


# Legacy alias for backward compat — old callsites that imported
# WuzapiDownloadImageRequest still work. New code should use the
# unified WuzapiDownloadMediaRequest name.
WuzapiDownloadImageRequest = WuzapiDownloadMediaRequest


class WuzapiDownloadResponse(BaseModel):
    """Output of ``POST /chat/downloadimage`` (and document/video/audio).

    ``data`` is a data URL like ``data:image/jpeg;base64,/9j/4AAQ...`` —
    callers decode the base64 payload to get the raw bytes. ``mimetype``
    is convenient when the caller wants to pick a file extension.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    mimetype: str = Field(alias="Mimetype")
    data: str = Field(alias="Data")


class WuzapiChatPresenceRequest(BaseModel):
    """Body for ``POST /chat/presence`` — typing/recording/paused indicator.

    ``phone`` accepts the same shapes as :class:`WuzapiSendTextRequest`
    (bare digits or full JID). ``state`` is one of:

    * ``composing`` — shows "typing..." in the recipient's chat
    * ``recording`` — shows "recording audio..." (we don't use this)
    * ``paused`` — clears the indicator immediately

    WhatsApp's typing indicator auto-clears after roughly 10 seconds of
    silence on the presence channel, so any agent that takes longer than
    that to compose needs the caller to refresh by re-sending
    ``composing`` periodically (see ``_TypingTracker`` in ``service.py``).

    ``media`` defaults to ``"text"``. Other valid value is ``"audio"`` for
    pairing with the ``recording`` state. We leave it as text.
    """

    model_config = ConfigDict(populate_by_name=True)

    phone: str
    state: str
    media: str = "text"


class WuzapiSetWebhookRequest(BaseModel):
    """Body for ``POST /webhook``.

    Two non-obvious wire-format facts (discovered against a live wuzapi):

    * Wire key is ``webhookurl`` — no underscore. Go's case-insensitive
      match doesn't bridge ``webhook_url`` to ``webhookurl``, so we use a
      ``serialization_alias`` to emit the right key while keeping a
      Pythonic field name.
    * ``events`` is ``[]string`` (array), NOT a comma-separated string.
      Passing a string returns HTTP 400 "could not decode payload".

    If ``events`` is omitted (None), wuzapi only updates the webhook URL
    and preserves the existing event filter.
    """

    model_config = ConfigDict(populate_by_name=True)

    webhook_url: str = Field(serialization_alias="webhookurl")
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
    subscribe: list[WuzapiEventName] = Field(
        default_factory=lambda: []  # noqa: PIE807 — typed empty default
    )


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

    ``info`` and ``raw_message`` are typed as ``dict[str, Any]`` deliberately:
    the underlying whatsmeow event has 25+ nested fields whose shapes vary
    by message kind (text, image, ephemeral, viewOnce, protocol, etc.).
    Modeling each as a TypedDict would be high-churn for low value when
    the adapter only reads a handful of keys with runtime narrowing. The
    ``Any`` here is honest — it's an opaque external-API payload.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    info: dict[str, Any] = Field(default_factory=dict, alias="Info")
    raw_message: dict[str, Any] = Field(default_factory=dict, alias="Message")


class WuzapiUnknownEvent(BaseModel):
    """Catch-all for event kinds the adapter doesn't yet handle.

    Receiver can log + drop rather than crashing.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class WuzapiWebhookEnvelope(BaseModel):
    """Top-level shape POSTed by wuzapi to the configured webhook URL.

    Discriminator for parsing: ``type`` field at the envelope level (one of
    the ``WuzapiEventName`` literals). Optional media-delivery fields
    (``base64``, ``s3``, ``mimeType``, ``fileName``) appear when the message
    has an attachment.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    type: str = ""
    event: dict[str, Any] = Field(default_factory=dict)
    token: str = ""
    base64: str | None = None
    mime_type: str | None = Field(default=None, alias="mimeType")
    file_name: str | None = Field(default=None, alias="fileName")


__all__ = [
    "WuzapiChatPresenceRequest",
    "WuzapiConnectRequest",
    "WuzapiConnectResponse",
    "WuzapiCreateUserRequest",
    "WuzapiDownloadImageRequest",
    "WuzapiDownloadMediaRequest",
    "WuzapiDownloadResponse",
    "WuzapiEventName",
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
