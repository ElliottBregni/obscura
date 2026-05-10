"""GatewayPollDaemon — concurrent poll loops for all registered platform adapters."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from obscura.gateway.network_bridge import GatewayNetworkBridge

logger = logging.getLogger(__name__)


class GatewayPollDaemon:
    """Polls iMessage, WhatsApp, and Discord concurrently and routes messages
    through GatewayNetworkBridge.

    Each registered adapter gets its own asyncio.Task running a poll loop.
    Errors in one platform's loop are logged and retried; they don't affect
    the other platforms.

    Usage::

        daemon = GatewayPollDaemon(bridge, poll_interval=2.0)
        daemon.register("imessage", imessage_adapter)
        daemon.register("whatsapp", whatsapp_adapter)
        daemon.register("discord", discord_adapter)
        await daemon.start()
        # ... runs until stopped ...
        await daemon.stop()

    Or as context manager::

        async with GatewayPollDaemon(bridge) as daemon:
            daemon.register("imessage", imessage_adapter)
            await asyncio.sleep(...)
    """

    def __init__(
        self,
        bridge: GatewayNetworkBridge,
        *,
        poll_interval: float = 2.0,
    ) -> None:
        self.bridge = bridge
        self._poll_interval = poll_interval
        self._running = False
        self._adapters: dict[str, Any] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._messages_dispatched: dict[str, int] = {}
        self._last_error: dict[str, str | None] = {}

    # ------------------------------------------------------------------
    # Adapter registration
    # ------------------------------------------------------------------

    def register(self, platform: str, adapter: Any) -> None:
        """Add an adapter; if the daemon is running, start its poll loop immediately.

        Args:
            platform: Platform identifier (e.g. ``"imessage"``, ``"whatsapp"``).
            adapter: Platform adapter with ``poll()`` and ``send()`` methods.
        """
        self._adapters[platform] = adapter
        self._messages_dispatched.setdefault(platform, 0)
        self._last_error.setdefault(platform, None)

        if self._running:
            task = asyncio.create_task(
                self._poll_loop(platform, adapter),
                name=f"poll-{platform}",
            )
            self._tasks[platform] = task
            logger.info("GatewayPollDaemon: started poll loop for %s (daemon already running)", platform)

    def deregister(self, platform: str) -> bool:
        """Cancel and remove a platform adapter.

        Args:
            platform: Platform identifier to remove.

        Returns:
            ``True`` if the platform was registered and removed, ``False`` otherwise.
        """
        if platform not in self._adapters:
            return False
        del self._adapters[platform]
        task = self._tasks.pop(platform, None)
        if task and not task.done():
            task.cancel()
        self._messages_dispatched.pop(platform, None)
        self._last_error.pop(platform, None)
        logger.info("GatewayPollDaemon: deregistered platform %s", platform)
        return True

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create tasks for all registered adapters; noop if already running."""
        if self._running:
            logger.debug("GatewayPollDaemon.start: already running, noop")
            return
        self._running = True
        for platform, adapter in self._adapters.items():
            task = asyncio.create_task(
                self._poll_loop(platform, adapter),
                name=f"poll-{platform}",
            )
            self._tasks[platform] = task
        logger.info(
            "GatewayPollDaemon started — %d platform(s): %s",
            len(self._adapters),
            ", ".join(self._adapters) or "(none)",
        )

    async def stop(self) -> None:
        """Cancel all tasks, wait for them, mark stopped."""
        if not self._running:
            logger.debug("GatewayPollDaemon.stop: not running, noop")
            return
        self._running = False
        for platform, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        logger.info("GatewayPollDaemon stopped")

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self, platform: str, adapter: Any) -> None:
        """Inner poll loop for a single platform.

        Runs until ``self._running`` is False.  Poll errors are caught and
        logged at WARNING level; per-message dispatch errors are caught and
        logged without stopping the loop.
        """
        logger.debug("GatewayPollDaemon: poll loop starting for %s", platform)
        while self._running:
            try:
                msgs = await adapter.poll()
                for msg in msgs:
                    try:
                        await self.bridge.dispatch(msg)
                        self._messages_dispatched[platform] = (
                            self._messages_dispatched.get(platform, 0) + 1
                        )
                        self._last_error[platform] = None
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "GatewayPollDaemon: dispatch error on %s (msg=%s): %s",
                            platform,
                            getattr(msg, "message_id", "?"),
                            exc,
                            exc_info=True,
                        )
                        self._last_error[platform] = str(exc)
            except asyncio.CancelledError:
                logger.debug("GatewayPollDaemon: poll loop cancelled for %s", platform)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "GatewayPollDaemon: poll error on %s: %s",
                    platform,
                    exc,
                    exc_info=True,
                )
                self._last_error[platform] = str(exc)

            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                logger.debug("GatewayPollDaemon: poll loop cancelled during sleep for %s", platform)
                return

        logger.debug("GatewayPollDaemon: poll loop exiting for %s", platform)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    async def get_status(self) -> dict[str, Any]:
        """Return daemon status including per-platform counters.

        Returns:
            A dict with ``running``, ``poll_interval``, and ``platforms`` keys.
            Each platform entry has ``task_alive``, ``messages_dispatched``,
            and ``last_error`` fields.
        """
        platforms: dict[str, dict[str, Any]] = {}
        for platform in self._adapters:
            task = self._tasks.get(platform)
            platforms[platform] = {
                "task_alive": task is not None and not task.done(),
                "messages_dispatched": self._messages_dispatched.get(platform, 0),
                "last_error": self._last_error.get(platform),
            }
        return {
            "running": self._running,
            "poll_interval": self._poll_interval,
            "platforms": platforms,
        }

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> GatewayPollDaemon:
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.stop()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


async def build_poll_daemon(
    bridge: GatewayNetworkBridge,
    *,
    poll_interval: float = 2.0,
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
) -> GatewayPollDaemon:
    """Build a GatewayPollDaemon with adapters for whichever platforms have credentials.

    Calls ``adapter.start()`` before registering.  Failed adapters are skipped
    with a warning.  Returns a started daemon (``start()`` is called before
    returning).

    Does NOT start the bridge — call ``bridge.start()`` separately.

    Args:
        bridge: A fully wired GatewayNetworkBridge instance.
        poll_interval: Polling interval in seconds for all adapters.
        imessage_contacts: Phone numbers / handles to monitor via iMessage.
            Pass ``None`` or empty list to skip iMessage.
        imessage_account_id: Logical account identifier for iMessage messages.
        whatsapp_contacts: Phone numbers to monitor via WhatsApp (Twilio).
            Pass ``None`` or empty list to skip WhatsApp.
        whatsapp_account_id: Logical account identifier for WhatsApp messages.
        whatsapp_account_sid: Twilio Account SID (falls back to env var).
        whatsapp_auth_token: Twilio Auth Token (falls back to env var).
        whatsapp_from_number: Twilio WhatsApp sender number (falls back to env var).
        discord_contacts: Discord channel IDs to monitor.
            Pass ``None`` or empty list to skip Discord.
        discord_account_id: Logical account identifier for Discord messages.
        discord_bot_token: Discord bot token (falls back to env var).

    Returns:
        A started GatewayPollDaemon with adapters registered for each platform
        whose credentials were valid.
    """
    daemon = GatewayPollDaemon(bridge, poll_interval=poll_interval)

    # ------------------------------------------------------------------
    # iMessage
    # ------------------------------------------------------------------
    if imessage_contacts:
        try:
            from obscura.integrations.imessage.adapter import IMessageAdapter  # noqa: PLC0415

            adapter = IMessageAdapter(
                contacts=imessage_contacts,
                account_id=imessage_account_id,
            )
            await adapter.start()
            # Register with router so the router can send replies back
            bridge.router.register("imessage", adapter)
            # Register with daemon so poll loop runs
            daemon.register("imessage", adapter)
            logger.info(
                "build_poll_daemon: iMessage adapter registered (%d contact(s))",
                len(imessage_contacts),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "build_poll_daemon: iMessage adapter failed to start, skipping: %s",
                exc,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # WhatsApp
    # ------------------------------------------------------------------
    if whatsapp_contacts:
        try:
            from obscura.integrations.whatsapp.adapter import WhatsAppAdapter  # noqa: PLC0415

            adapter = WhatsAppAdapter(
                contacts=whatsapp_contacts,
                account_id=whatsapp_account_id,
                account_sid=whatsapp_account_sid,
                auth_token=whatsapp_auth_token,
                from_number=whatsapp_from_number,
            )
            await adapter.start()
            bridge.router.register("whatsapp", adapter)
            daemon.register("whatsapp", adapter)
            logger.info(
                "build_poll_daemon: WhatsApp adapter registered (%d contact(s))",
                len(whatsapp_contacts),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "build_poll_daemon: WhatsApp adapter failed to start, skipping: %s",
                exc,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Discord
    # ------------------------------------------------------------------
    if discord_contacts:
        try:
            from obscura.integrations.discord.adapter import DiscordAdapter  # noqa: PLC0415

            adapter = DiscordAdapter(
                contacts=discord_contacts,
                account_id=discord_account_id,
                bot_token=discord_bot_token,
            )
            await adapter.start()
            bridge.router.register("discord", adapter)
            daemon.register("discord", adapter)
            logger.info(
                "build_poll_daemon: Discord adapter registered (%d channel(s))",
                len(discord_contacts),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "build_poll_daemon: Discord adapter failed to start, skipping: %s",
                exc,
                exc_info=True,
            )

    await daemon.start()
    return daemon
