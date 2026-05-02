"""ChannelDaemon — polls all enabled channel adapters and routes messages.

Zero-config polling mode: no open ports, no webhooks needed.
Works behind NAT, firewall, or localhost.

Usage::

    from obscura.agent.channel_daemon import ChannelDaemon, ChannelDaemonConfig
    from obscura.integrations.messaging.router import ChannelRouter, ChannelRouterConfig, ObscuraAgentRunner

    runner = ObscuraAgentRunner(backend=my_backend, tool_registry=my_tools)
    router = ChannelRouter(runner=runner, config=ChannelRouterConfig())

    daemon = ChannelDaemon(router=router, config=ChannelDaemonConfig(poll_interval_seconds=5))
    daemon.add_adapter("telegram", telegram_adapter)
    daemon.add_adapter("whatsapp", whatsapp_adapter)

    await daemon.run()   # loops forever, ctrl+c to stop

CLI shortcut (coming soon):
    obscura channels start --mode polling
"""

from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.integrations.messaging.models import PlatformMessage
    from obscura.integrations.messaging.router import ChannelRouter

logger = logging.getLogger(__name__)


@dataclass
class ChannelDaemonConfig:
    """Configuration for the ChannelDaemon polling loop."""

    poll_interval_seconds: float = 5.0
    error_backoff_seconds: float = 30.0
    # If True, send a typing indicator before running agent (Telegram only)
    send_typing: bool = True


class ChannelDaemon:
    """Background daemon that polls all registered adapters for new messages.

    For each adapter, it calls adapter.poll() at the configured interval,
    then dispatches each PlatformMessage through the ChannelRouter.
    """

    def __init__(
        self,
        router: ChannelRouter,
        *,
        config: ChannelDaemonConfig | None = None,
    ) -> None:
        self._router = router
        self._config = config or ChannelDaemonConfig()
        self._adapters: dict[str, Any] = {}
        self._running = False

    def add_adapter(self, platform: str, adapter: Any) -> None:
        """Register a polling adapter and wire it into the router."""
        self._adapters[platform.lower()] = adapter
        self._router.register(platform, adapter)
        logger.info("ChannelDaemon: registered %s adapter", platform)

    async def start_adapters(self) -> None:
        """Call start() on all registered adapters."""
        for platform, adapter in self._adapters.items():
            try:
                await adapter.start()
                logger.info("ChannelDaemon: %s adapter started", platform)
            except Exception:
                logger.exception("ChannelDaemon: failed to start %s adapter", platform)

    async def run(self) -> None:
        """Main poll loop — runs until stopped or KeyboardInterrupt."""
        self._running = True
        await self.start_adapters()

        loop = asyncio.get_running_loop()

        def _stop() -> None:
            logger.info("ChannelDaemon: shutdown signal received")
            self._running = False

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except (NotImplementedError, RuntimeError):
                pass  # Windows / non-main thread

        logger.info(
            "ChannelDaemon: polling %d adapters every %.1fs",
            len(self._adapters),
            self._config.poll_interval_seconds,
        )

        while self._running:
            tasks = [
                self._poll_once(platform, adapter)
                for platform, adapter in self._adapters.items()
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(self._config.poll_interval_seconds)

        logger.info("ChannelDaemon: stopped")

    async def _poll_once(self, platform: str, adapter: Any) -> None:
        """Poll one adapter and dispatch all new messages."""
        try:
            messages: list[PlatformMessage] = await adapter.poll()
        except Exception:
            logger.exception("ChannelDaemon: poll error on %s, backing off", platform)
            await asyncio.sleep(self._config.error_backoff_seconds)
            return

        if messages:
            logger.info(
                "ChannelDaemon: %d new message(s) on %s", len(messages), platform
            )

        for msg in messages:
            try:
                await self._router.dispatch_message(msg)
            except Exception:
                logger.exception(
                    "ChannelDaemon: dispatch error for message_id=%s", msg.message_id
                )
