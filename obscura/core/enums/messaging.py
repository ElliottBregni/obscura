"""Messaging enums — channel routing and push delivery.

Values are persisted in the ``ChannelConfigRecord`` row (``mode`` column)
and shipped as request bodies to APNs/FCM/Expo push endpoints, so they are
load-bearing wire strings.
"""

from __future__ import annotations

from enum import StrEnum


class ChannelMode(StrEnum):
    """Execution mode for a messaging channel.

    ``CHAT`` — standard single-turn AgentLoop.
    ``KAIROS`` — long-horizon goal runtime.
    ``CHANNEL_INJECT`` — inject into the active REPL session instead of running
    an autonomous agent; Claude's reply is sent back to the originating platform.
    """

    CHAT = "chat"
    KAIROS = "kairos"
    CHANNEL_INJECT = "channel_inject"


class PushProvider(StrEnum):
    """Outbound push delivery provider."""

    APNS = "apns"
    FCM = "fcm"
    EXPO = "expo"


class TriggerKind(StrEnum):
    """Discriminator for daemon-agent trigger payloads.

    ``STOP`` keeps its legacy ``__stop__`` magic-string value so existing
    queue producers (``DaemonAgent.stop()``) round-trip byte-identically.
    """

    IMESSAGE = "imessage"
    MESSAGE = "message"
    EMAIL = "email"
    STOP = "__stop__"
