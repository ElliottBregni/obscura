"""MessagePlatformAdapter implementation backed by the wuzapi sidecar.

This adapter is webhook-driven, not polling-driven. ``poll()`` always
returns an empty list — kept for protocol compatibility with the existing
``ChannelDaemon`` interface. Inbound flow:

  wuzapi POST → :class:`build_webhook_app` → :meth:`WuzapiAdapter.handle_event`
                                            → :class:`PlatformMessage`

The caller (REPL startup wiring) is responsible for hosting the webhook
server on a chosen loopback port and forwarding parsed envelopes to
:meth:`handle_event`. That separation keeps the adapter free of HTTP
server lifecycle concerns.

Outbound flow uses the typed :class:`WuzapiClient` directly.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Final

from obscura.integrations.messaging.identity import normalize_identity
from obscura.integrations.messaging.models import PlatformMessage
from obscura.integrations.whatsapp.wuzapi.client import WuzapiClient, WuzapiError
from obscura.integrations.whatsapp.wuzapi.models import (
    WuzapiSendTextRequest,
    WuzapiWebhookEnvelope,
)

logger = logging.getLogger(__name__)

_PLATFORM: Final[str] = "whatsapp"
_JID_SUFFIXES: Final[tuple[str, ...]] = (
    "@s.whatsapp.net",
    "@g.us",
    "@lid",
    "@broadcast",
)


# ---------------------------------------------------------------------------
# JID helpers
# ---------------------------------------------------------------------------


def _strip_jid(jid: str) -> str:
    """Convert a wuzapi JID into a normalized identity.

    Only direct-message JIDs (``@s.whatsapp.net``) get the ``+`` phone-number
    prefix; group and LID JIDs are opaque identifiers, not numbers.

    Examples::

        "12316333624:14@s.whatsapp.net"  -> "+12316333624"
        "12316333624@s.whatsapp.net"     -> "+12316333624"
        "120363177012345678@g.us"        -> "120363177012345678"  # group
        "187437204672730@lid"            -> "187437204672730"     # LID
        ""                                -> "unknown"

    The trailing ``:<device>`` suffix on direct-message JIDs identifies the
    sending linked-device; we drop it for identity purposes since we want
    one identity per WhatsApp account.
    """
    if not jid:
        return "unknown"
    bare = jid
    matched_suffix: str | None = None
    for suffix in _JID_SUFFIXES:
        if bare.endswith(suffix):
            bare = bare[: -len(suffix)]
            matched_suffix = suffix
            break
    if ":" in bare:
        bare = bare.split(":", 1)[0]
    if matched_suffix == "@s.whatsapp.net" and bare.isdigit():
        return normalize_identity(f"+{bare}")
    return normalize_identity(bare)


def _extract_text(message: dict[str, object]) -> str:
    """Pull text from the wuzapi Message dict.

    wuzapi forwards whatsmeow's union shape verbatim, so the text can live
    in any of several keys depending on the message variant:

    * ``conversation``                                 — plain text
    * ``extendedTextMessage.text``                     — replies, forwards
    * ``ephemeralMessage.message.conversation``        — disappearing msgs
    * ``ephemeralMessage.message.extendedTextMessage.text``
    * ``viewOnceMessage.message.{conversation|extendedTextMessage.text}``

    We probe these in order. If none match, we log + return an empty
    string so the caller can decide whether to drop the event.
    """

    def _from_inner(inner: object) -> str:
        if not isinstance(inner, dict):
            return ""
        if isinstance(inner.get("conversation"), str):
            return str(inner["conversation"])
        ext = inner.get("extendedTextMessage")
        if isinstance(ext, dict) and isinstance(ext.get("text"), str):
            return str(ext["text"])
        return ""

    # Top-level forms
    direct = _from_inner(message)
    if direct:
        return direct
    # Wrapper forms — peel one or two layers
    for wrapper in ("ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2"):
        outer = message.get(wrapper)
        if isinstance(outer, dict):
            inner_msg = outer.get("message")
            text = _from_inner(inner_msg) if isinstance(inner_msg, dict) else ""
            if text:
                return text
    logger.debug("wuzapi: no text extractable from message variant: %s",
                 sorted(message.keys()))
    return ""


def _parse_ts(info: dict[str, object]) -> datetime:
    """Parse the wuzapi Info.Timestamp ISO8601 string into a UTC datetime."""
    raw = info.get("Timestamp")
    if not isinstance(raw, str):
        return datetime.now(tz=UTC)
    try:
        # whatsmeow emits e.g. "2026-05-11T18:07:45-04:00"
        return datetime.fromisoformat(raw).astimezone(UTC)
    except ValueError:
        logger.warning("wuzapi: unparseable timestamp %r, defaulting to now", raw)
        return datetime.now(tz=UTC)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class WuzapiAdapter:
    """Adapter from wuzapi events to obscura's :class:`PlatformMessage`.

    Construction is cheap — pass a configured :class:`WuzapiClient`. Call
    :meth:`start` once to verify the WhatsApp session is linked before
    treating the adapter as ready.

    :param client: An owned :class:`WuzapiClient` for HTTP traffic. The
        adapter does not manage the client's lifecycle — caller closes it.
    :param account_id: Stable label for this WhatsApp account inside
        obscura's conversation state store. Default ``"default"``.
    :param drop_from_me: If ``True`` (default), inbound events where
        ``IsFromMe == True`` are ignored. This prevents echoes from our
        own linked-device sends from looping back into the agent.
    :param drop_non_text: If ``True`` (default), events whose extracted
        text is empty are ignored. Set to ``False`` if you want
        placeholders for media messages.
    """

    def __init__(
        self,
        client: WuzapiClient,
        *,
        account_id: str = "default",
        drop_from_me: bool = True,
        drop_non_text: bool = True,
    ) -> None:
        self._client = client
        self._account_id = account_id
        self._drop_from_me = drop_from_me
        self._drop_non_text = drop_non_text

    # ---------- lifecycle ----------

    async def start(self) -> None:
        """Verify the WhatsApp session is linked before declaring ready.

        Raises :class:`RuntimeError` if the session is connected to wuzapi
        but no WhatsApp account is actually linked (loggedIn=false).
        """
        status = await self._client.session_status()
        if not status.logged_in:
            raise RuntimeError(
                f"wuzapi session not linked (connected={status.connected}). "
                "Run `obscura whatsapp link` to scan QR and link your account."
            )
        logger.info("wuzapi adapter ready: jid=%s account_id=%s",
                    status.jid, self._account_id)

    # ---------- inbound (polling protocol, kept for compat) ----------

    async def poll(self) -> list[PlatformMessage]:
        """Always returns ``[]`` — wuzapi is webhook-driven, not pollable.

        Kept for symmetry with the existing ``MessagePlatformAdapter``
        protocol so ``ChannelDaemon`` and friends can treat both transports
        uniformly. Inbound goes through :meth:`handle_event` instead.
        """
        return []

    # ---------- outbound ----------

    async def send(self, recipient: str, text: str) -> bool:
        """Send a text message to ``recipient`` (a phone number, with or without ``+``).

        Returns ``True`` on successful wuzapi acknowledgement, ``False``
        on any transport error (logged at WARNING).
        """
        phone = re.sub(r"\D", "", recipient)
        if not phone:
            logger.warning("wuzapi.send: empty recipient after digit-strip: %r", recipient)
            return False
        try:
            resp = await self._client.send_text(
                WuzapiSendTextRequest(phone=phone, body=text)
            )
        except WuzapiError:
            logger.exception("wuzapi.send to %s failed", phone)
            return False
        logger.info("wuzapi.send id=%s to=%s", resp.id, phone)
        return True

    # ---------- webhook event handling ----------

    def handle_event(
        self, envelope: WuzapiWebhookEnvelope
    ) -> PlatformMessage | None:
        """Convert a wuzapi webhook envelope to a :class:`PlatformMessage`.

        Returns ``None`` if the event isn't a normal inbound text we should
        forward (own message, non-text payload, unknown event type, etc.).
        Never raises — invalid shapes are logged and dropped.
        """
        if envelope.type != "Message":
            logger.debug("wuzapi: skipping non-Message event %r", envelope.type)
            return None

        info = envelope.event.get("Info") if isinstance(envelope.event, dict) else None
        msg = envelope.event.get("Message") if isinstance(envelope.event, dict) else None
        if not isinstance(info, dict) or not isinstance(msg, dict):
            logger.warning("wuzapi: malformed Message envelope (missing Info/Message)")
            return None

        if self._drop_from_me and bool(info.get("IsFromMe")):
            logger.debug("wuzapi: skipping IsFromMe=true echo")
            return None

        text = _extract_text(msg)
        if self._drop_non_text and not text:
            logger.debug("wuzapi: skipping empty-text event")
            return None

        sender_jid = str(info.get("Sender") or "")
        chat_jid = str(info.get("Chat") or "")
        is_group = bool(info.get("IsGroup"))
        message_id = str(info.get("ID") or "")

        sender_id = _strip_jid(sender_jid)
        if is_group:
            channel_id = f"group:{_strip_jid(chat_jid)}"
            recipient_id = "group"
        else:
            channel_id = f"dm:{sender_id}"
            recipient_id = "me"

        if not message_id:
            # Synthesize a stable ID for dedup
            message_id = hashlib.sha1(
                f"{sender_jid}|{info.get('Timestamp', '')}|{text}".encode()
            ).hexdigest()

        metadata: dict[str, Any] = {
            "wuzapi_message_id": message_id,
            "jid_sender": sender_jid,
            "jid_chat": chat_jid,
            "is_group": is_group,
            "is_from_me": bool(info.get("IsFromMe")),
            "message_type": info.get("Type"),
            "push_name": info.get("PushName"),
        }

        return PlatformMessage(
            platform=_PLATFORM,
            account_id=self._account_id,
            channel_id=channel_id,
            sender_id=sender_id,
            recipient_id=recipient_id,
            message_id=message_id,
            text=text,
            timestamp=_parse_ts(info),
            metadata=metadata,
        )

    def handle_events(
        self, envelopes: Iterable[WuzapiWebhookEnvelope]
    ) -> list[PlatformMessage]:
        """Batch convenience over :meth:`handle_event`."""
        return [m for e in envelopes if (m := self.handle_event(e)) is not None]


__all__ = ["WuzapiAdapter"]
