"""Gateway platform adapter wrappers.

Thin helpers that build real platform adapters (IMessageAdapter,
WhatsAppAdapter, DiscordAdapter) and register them with a ChannelRouter.

All three real adapter classes already implement the ChannelAdapter protocol
(``async def send(recipient: str, text: str) -> bool``) — no wrapper class
needed.  This module provides:

* ``build_imessage_adapter`` / ``build_whatsapp_adapter`` / ``build_discord_adapter``
  — thin factories with explicit kwargs so callers don't need to know which
    underlying adapter class to import.

* ``register_platform_adapters`` — convenience function that calls whichever
  factories have enough credentials and registers the resulting adapters with
  a ChannelRouter.

Usage::

    from obscura.gateway.adapters import register_platform_adapters

    await register_platform_adapters(
        router,
        imessage_contacts=["+14155550123"],
        whatsapp_contacts=["+14155550456"],
        whatsapp_account_sid="AC...",
        whatsapp_auth_token="...",
        whatsapp_from_number="whatsapp:+14155550789",
        discord_contacts=["1234567890"],
        discord_bot_token="Bot ...",
    )
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.integrations.messaging.router import ChannelRouter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Individual adapter builders
# ---------------------------------------------------------------------------


def build_imessage_adapter(
    contacts: list[str],
    *,
    account_id: str = "default",
) -> Any:
    """Build an IMessageAdapter.

    Args:
        contacts: List of phone numbers / handles to allow.
        account_id: Logical account label surfaced in PlatformMessage.

    Returns:
        An IMessageAdapter implementing ChannelAdapter.
    """
    from obscura.integrations.imessage.adapter import IMessageAdapter  # noqa: PLC0415

    return IMessageAdapter(contacts, account_id=account_id)


def build_whatsapp_adapter(
    contacts: list[str],
    *,
    account_id: str = "default",
    account_sid: str | None = None,
    auth_token: str | None = None,
    from_number: str | None = None,
) -> Any:
    """Build a WhatsAppAdapter (Twilio-backed).

    Args:
        contacts: List of phone numbers to allow.
        account_id: Logical account label surfaced in PlatformMessage.
        account_sid: Twilio account SID.
        auth_token: Twilio auth token.
        from_number: Twilio WhatsApp sender number (``whatsapp:+1...``).

    Returns:
        A WhatsAppAdapter implementing ChannelAdapter.
    """
    from obscura.integrations.whatsapp.adapter import WhatsAppAdapter  # noqa: PLC0415

    return WhatsAppAdapter(
        contacts,
        account_id=account_id,
        account_sid=account_sid,
        auth_token=auth_token,
        from_number=from_number,
    )


def build_discord_adapter(
    contacts: list[str],
    *,
    account_id: str = "default",
    bot_token: str | None = None,
) -> Any:
    """Build a DiscordAdapter.

    Args:
        contacts: Discord channel IDs to poll / send to.
        account_id: Logical account label surfaced in PlatformMessage.
        bot_token: Discord bot token.  Falls back to ``DISCORD_BOT_TOKEN`` env var.

    Returns:
        A DiscordAdapter implementing ChannelAdapter.
    """
    from obscura.integrations.discord.adapter import DiscordAdapter  # noqa: PLC0415

    return DiscordAdapter(contacts, account_id=account_id, bot_token=bot_token)


# ---------------------------------------------------------------------------
# Bulk registration helper
# ---------------------------------------------------------------------------


async def register_platform_adapters(
    router: ChannelRouter,
    *,
    # iMessage
    imessage_contacts: list[str] | None = None,
    imessage_account_id: str = "default",
    # WhatsApp
    whatsapp_contacts: list[str] | None = None,
    whatsapp_account_id: str = "default",
    whatsapp_account_sid: str | None = None,
    whatsapp_auth_token: str | None = None,
    whatsapp_from_number: str | None = None,
    # Discord
    discord_contacts: list[str] | None = None,
    discord_account_id: str = "default",
    discord_bot_token: str | None = None,
    # Generic fallback kwargs (ignored, kept for forward-compat)
    **_adapter_kwargs: Any,
) -> None:
    """Register whichever platform adapters have sufficient credentials.

    Adapters that cannot be built (missing required creds, import error, etc.)
    are skipped with a warning — the remaining adapters are still registered.

    Args:
        router: The ChannelRouter to register adapters into.
        imessage_contacts: Phone numbers / handles for iMessage.  If None,
            iMessage adapter is skipped.
        imessage_account_id: Account label for iMessage PlatformMessages.
        whatsapp_contacts: Phone numbers for WhatsApp.  If None, skipped.
        whatsapp_account_id: Account label for WhatsApp PlatformMessages.
        whatsapp_account_sid: Twilio account SID.
        whatsapp_auth_token: Twilio auth token.
        whatsapp_from_number: Twilio sender number.
        discord_contacts: Discord channel IDs.  If None, skipped.
        discord_account_id: Account label for Discord PlatformMessages.
        discord_bot_token: Discord bot token.
    """
    # --- iMessage ---
    if imessage_contacts is not None:
        try:
            adapter = build_imessage_adapter(
                imessage_contacts,
                account_id=imessage_account_id,
            )
            # Call start() to verify AppleScript / DB access
            if hasattr(adapter, "start"):
                await adapter.start()
            router.register("imessage", adapter)
            logger.info(
                "register_platform_adapters: iMessage registered (%d contact(s))",
                len(imessage_contacts),
            )
        except Exception:
            logger.warning(
                "register_platform_adapters: iMessage adapter failed to initialise; skipping",
                exc_info=True,
            )

    # --- WhatsApp ---
    if whatsapp_contacts is not None:
        if not (whatsapp_account_sid and whatsapp_auth_token):
            logger.warning(
                "register_platform_adapters: WhatsApp contacts provided but "
                "whatsapp_account_sid / whatsapp_auth_token are missing; skipping"
            )
        else:
            try:
                adapter = build_whatsapp_adapter(
                    whatsapp_contacts,
                    account_id=whatsapp_account_id,
                    account_sid=whatsapp_account_sid,
                    auth_token=whatsapp_auth_token,
                    from_number=whatsapp_from_number,
                )
                if hasattr(adapter, "start"):
                    await adapter.start()
                router.register("whatsapp", adapter)
                logger.info(
                    "register_platform_adapters: WhatsApp registered (%d contact(s))",
                    len(whatsapp_contacts),
                )
            except Exception:
                logger.warning(
                    "register_platform_adapters: WhatsApp adapter failed to initialise; skipping",
                    exc_info=True,
                )

    # --- Discord ---
    if discord_contacts is not None:
        try:
            adapter = build_discord_adapter(
                discord_contacts,
                account_id=discord_account_id,
                bot_token=discord_bot_token,
            )
            if hasattr(adapter, "start"):
                await adapter.start()
            router.register("discord", adapter)
            logger.info(
                "register_platform_adapters: Discord registered (%d channel(s))",
                len(discord_contacts),
            )
        except Exception:
            logger.warning(
                "register_platform_adapters: Discord adapter failed to initialise; skipping",
                exc_info=True,
            )
