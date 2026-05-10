"""ChannelRouter — routes inbound platform messages directly into Obscura's agent loop.

No external bridge required. Messages from Telegram, WhatsApp, Signal, etc.
are normalized to PlatformMessage, mapped to a per-user ConversationState,
and dispatched directly into the agent loop (v2). Responses are sent back
via the originating platform adapter.

Usage::

    from obscura.integrations.messaging.router import ChannelRouter, ChannelRouterConfig
    from obscura.integrations.messaging.store import ConversationStore
    from obscura.core.agent_loop_factory import make_agent_loop

    config = ChannelRouterConfig(
        system_prompt="You are a helpful assistant.",
        session_timeout_seconds=3600,
    )
    router = ChannelRouter(runner=runner, config=config)

    # Register adapters
    router.register("telegram", telegram_adapter)
    router.register("whatsapp", whatsapp_adapter)

    # Optionally put telegram in KAIROS mode
    router.set_platform_mode("telegram", ChannelMode.KAIROS, kairos_runner=kairos_runner)

    # Dispatch a message (called from webhook handlers or poll loop)
    await router.dispatch(platform="telegram", sender_id="123456", text="hello")
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

from obscura.core.enums.messaging import ChannelMode
from obscura.core.paths import resolve_obscura_home
from obscura.integrations.messaging.identity import build_conversation_key
from obscura.integrations.messaging.kairos_runner import (
    KairosAgentRunner,
    KairosRunnerConfig,
)
from obscura.integrations.messaging.models import PlatformMessage
from obscura.integrations.messaging.runners import (
    AgentRunnerProtocol as AgentRunnerProtocol,
    ObscuraAgentRunner as ObscuraAgentRunner,
)
from obscura.integrations.messaging.store import (
    ChannelConfigRecord,
    ConversationStore,
    MessageDedupeStore,
)


logger = logging.getLogger(__name__)

_MAX_REPLY_LEN: int = 4_000  # chars — platform reply size cap (WhatsApp/Telegram limit ~4096)


# ---------------------------------------------------------------------------
# Adapter protocol (subset needed by the router)
# ---------------------------------------------------------------------------


class ChannelAdapter(Protocol):
    """Minimal interface the router requires from any channel adapter."""

    async def send(self, recipient: str, text: str) -> bool:
        """Send a plain-text reply to the platform user."""
        ...


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class ChannelRouterConfig:
    """Configuration for ChannelRouter behaviour."""

    system_prompt: str = (
        "You are a helpful assistant. Be concise. You are replying via a messaging app."
    )
    max_turns: int = 8
    session_timeout_seconds: float = 3600.0  # Reset conversation after 1h idle
    max_history_entries: int = 40
    # If True, sends a "typing..." indicator before running agent (Telegram only)
    send_typing_indicator: bool = True
    # Maximum concurrent dispatches (per-router)
    max_concurrent: int = 10
    # Account ID label for ConversationStore entries
    account_id: str = "channel"
    # Default execution mode for all platforms (can be overridden per-platform)
    mode: ChannelMode = ChannelMode.CHAT


# AgentRunnerProtocol and ObscuraAgentRunner now live in
# obscura.integrations.messaging.runners (re-exported above) so that
# kairos_runner can import the concrete class from a leaf module without
# forming a peer cycle with this file.


# ---------------------------------------------------------------------------
# ChannelRouter
# ---------------------------------------------------------------------------


class ChannelRouter:
    """Routes inbound PlatformMessages to Obscura agents and sends replies back.

    Architecture:
        Inbound message
            → build conversation_key (platform + sender_id)
            → deduplicate (MessageDedupeStore)
            → load/create ConversationState
            → reset if stale (session_timeout)
            → append user turn to history
            → run agent via per-platform runner (chat or kairos mode)
            → append assistant reply to history
            → send reply via adapter
    """

    def __init__(
        self,
        runner: AgentRunnerProtocol,
        *,
        config: ChannelRouterConfig | None = None,
        store: ConversationStore | None = None,
        dedupe: MessageDedupeStore | None = None,
    ) -> None:
        self._runner = runner
        self._config = config or ChannelRouterConfig()
        self._store = store or ConversationStore()
        self._dedupe = dedupe or MessageDedupeStore()
        self._adapters: dict[str, ChannelAdapter] = {}
        self._semaphore = asyncio.Semaphore(self._config.max_concurrent)
        # Per-platform mode overrides and dedicated runners
        self._platform_modes: dict[str, ChannelMode] = {}
        self._platform_runners: dict[str, AgentRunnerProtocol] = {}

    # ------------------------------------------------------------------
    # Adapter registration
    # ------------------------------------------------------------------

    def register(self, platform: str, adapter: ChannelAdapter) -> None:
        """Register a platform adapter for sending replies."""
        self._adapters[platform.lower()] = adapter
        logger.info("ChannelRouter: registered adapter for platform=%s", platform)

    def deregister(self, platform: str) -> bool:
        """Remove a platform adapter and its mode override; returns True if one was present."""
        key = platform.lower()
        removed = self._adapters.pop(key, None)
        self._platform_modes.pop(key, None)
        self._platform_runners.pop(key, None)
        if removed is not None:
            logger.info("ChannelRouter: deregistered adapter for platform=%s", platform)
            return True
        return False

    # ------------------------------------------------------------------
    # Per-platform mode routing
    # ------------------------------------------------------------------

    def set_platform_mode(
        self,
        platform: str,
        mode: ChannelMode,
        *,
        kairos_runner: AgentRunnerProtocol | None = None,
    ) -> None:
        """Set the execution mode for a specific platform.

        When *mode* is ``KAIROS``, supply a *kairos_runner* instance.
        If none is provided the platform silently falls back to ``CHAT``.

        When *mode* is ``CHANNEL_INJECT``, no runner is required — inbound
        messages are pushed into the shared REPL inject queue instead.
        """
        key = platform.lower()
        if mode == ChannelMode.KAIROS and kairos_runner is not None:
            self._platform_modes[key] = ChannelMode.KAIROS
            self._platform_runners[key] = kairos_runner
        elif mode == ChannelMode.CHANNEL_INJECT:
            self._platform_modes[key] = ChannelMode.CHANNEL_INJECT
            self._platform_runners.pop(key, None)
        else:
            if mode == ChannelMode.KAIROS:
                logger.warning(
                    "set_platform_mode: KAIROS requested for %s but no kairos_runner "
                    "supplied — falling back to CHAT",
                    platform,
                )
            self._platform_modes[key] = ChannelMode.CHAT
            self._platform_runners.pop(key, None)

    def get_platform_mode(self, platform: str) -> ChannelMode:
        """Return the effective mode for *platform*."""
        return self._platform_modes.get(platform.lower(), self._config.mode)

    def _get_runner_for(self, platform: str) -> AgentRunnerProtocol:
        """Return the runner that should handle messages for *platform*."""
        key = platform.lower()
        mode = self._platform_modes.get(key, self._config.mode)
        if mode == ChannelMode.KAIROS:
            runner = self._platform_runners.get(key)
            if runner is not None:
                return runner
        return self._runner

    # ------------------------------------------------------------------
    # Hot-reload from DB config
    # ------------------------------------------------------------------

    async def apply_config(self, record: "Any") -> None:
        """Hot-reload: build and register (or deregister) an adapter from a ChannelConfigRecord.

        Called at runtime when a config is created/updated/applied via the REST API.
        Raises ValueError for unknown platforms; logs and swallows adapter init errors.
        """
        if not isinstance(record, ChannelConfigRecord):
            msg = f"apply_config requires a ChannelConfigRecord, got {type(record)}"
            raise TypeError(msg)

        platform = record.platform.lower()

        if not record.enabled:
            self.deregister(platform)
            return

        creds = record.credentials
        contacts = record.contacts

        if platform == "telegram":
            bot_token = creds.get("bot_token", "")
            if not bot_token:
                msg = "Telegram config missing 'bot_token' in credentials"
                raise ValueError(msg)
            from obscura.integrations.telegram.adapter import TelegramAdapter

            adapter: ChannelAdapter = TelegramAdapter(
                contacts=contacts,
                bot_token=bot_token,
                webhook_secret=creds.get("webhook_secret"),
            )
            self.register(platform, adapter)

        elif platform == "whatsapp":
            account_sid = creds.get("account_sid", "")
            auth_token = creds.get("auth_token", "")
            from_number = creds.get("from_number", "")
            if not account_sid or not auth_token:
                msg = (
                    "WhatsApp config missing 'account_sid'/'auth_token' in credentials"
                )
                raise ValueError(msg)
            from obscura.integrations.whatsapp.adapter import WhatsAppAdapter

            wa_adapter: ChannelAdapter = WhatsAppAdapter(
                contacts=contacts,
                account_sid=account_sid,
                auth_token=auth_token,
                from_number=from_number,
            )
            self.register(platform, wa_adapter)

        else:
            msg = f"apply_config: unsupported platform '{platform}'"
            raise ValueError(msg)

        # Apply the execution mode stored in the config record
        record_mode = getattr(record, "mode", "chat") or "chat"
        try:
            channel_mode = ChannelMode(record_mode.lower())
        except ValueError:
            logger.debug("suppressed exception in apply_config", exc_info=True)
            channel_mode = ChannelMode.CHAT

        if channel_mode == ChannelMode.KAIROS:
            try:
                _base: Any = self._runner
                kairos_runner: AgentRunnerProtocol = KairosAgentRunner(
                    backend=getattr(_base, "_backend", None),
                    tool_registry=getattr(_base, "_tool_registry", None),
                    config=KairosRunnerConfig(
                        db_path=resolve_obscura_home() / "kairos.db",
                    ),
                )
                self.set_platform_mode(
                    platform, ChannelMode.KAIROS, kairos_runner=kairos_runner
                )
            except Exception:
                logger.warning(
                    "KAIROS runner init failed for platform=%s; using CHAT mode",
                    platform,
                    exc_info=True,
                )
                self.set_platform_mode(platform, ChannelMode.CHAT)
        else:
            self.set_platform_mode(platform, ChannelMode.CHAT)

        logger.info(
            "ChannelRouter: hot-reloaded adapter for platform=%s config_id=%s mode=%s",
            platform,
            record.id,
            channel_mode.value,
        )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch_message(self, msg: PlatformMessage) -> None:
        """Dispatch a fully normalized PlatformMessage through the agent loop."""
        async with self._semaphore:
            await self._handle(msg)

    async def dispatch(
        self,
        *,
        platform: str,
        sender_id: str,
        text: str,
        channel_id: str | None = None,
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Convenience: build a PlatformMessage and dispatch it."""
        _channel_id = channel_id or f"dm:{sender_id}"
        _message_id = (
            message_id
            or hashlib.sha1(
                f"{platform}:{sender_id}:{time.time()}".encode()
            ).hexdigest()
        )

        msg = PlatformMessage(
            platform=platform,
            account_id=self._config.account_id,
            channel_id=_channel_id,
            sender_id=sender_id,
            recipient_id="agent",
            message_id=_message_id,
            text=text,
            timestamp=datetime.datetime.now(tz=datetime.UTC),
            metadata=metadata or {},
        )
        await self.dispatch_message(msg)

    async def _handle(self, msg: PlatformMessage) -> None:
        platform = msg.platform.lower()
        sender_id = msg.sender_id

        # Deduplicate
        if not self._dedupe.add_if_absent(msg.message_id):
            logger.debug("Duplicate message_id=%s, skipping", msg.message_id)
            return

        # Build stable conversation key
        conv_key = build_conversation_key(
            platform=platform,
            account_id=msg.account_id,
            channel_id=msg.channel_id,
            participants=[sender_id],
        )

        # Ensure conversation exists in store
        self._store.ensure(
            conversation_key=conv_key,
            platform=platform,
            account_id=msg.account_id,
            channel_id=msg.channel_id,
            participants=[sender_id],
        )

        # Reset history if conversation is stale
        if self._config.session_timeout_seconds > 0:
            self._store.reset_if_stale(conv_key, self._config.session_timeout_seconds)

        # Append user message to history
        state = self._store.append_user_message(
            conv_key,
            msg.text,
            max_history_entries=self._config.max_history_entries,
        )

        # Send typing indicator if supported
        adapter = self._adapters.get(platform)
        if adapter and self._config.send_typing_indicator:
            try:
                if hasattr(adapter, "send_typing"):
                    chat_id = msg.metadata.get("chat_id", sender_id)
                    await adapter.send_typing(chat_id)  # type: ignore[attr-defined]
            except Exception:
                logger.debug("suppressed exception in _handle", exc_info=True)

        # Channel-inject mode: push to REPL queue instead of running autonomous agent
        mode = self.get_platform_mode(platform)
        if mode == ChannelMode.CHANNEL_INJECT:
            from obscura.integrations.messaging.channel_inject import (
                ChannelMessage,
                push_channel_message,
            )

            _adapter = self._adapters.get(platform)

            async def _reply(text: str) -> bool:
                if _adapter is None:
                    return False
                # Truncate reply to platform message limit
                if len(text) > _MAX_REPLY_LEN:
                    text = text[:_MAX_REPLY_LEN] + "… [reply truncated]"
                try:
                    return await _adapter.send(sender_id, text)
                except Exception:
                    logger.warning(
                        "channel_inject: reply send failed for %s/%s",
                        platform,
                        sender_id,
                    )
                    return False

            pushed = push_channel_message(
                ChannelMessage(
                    platform=platform,
                    sender_id=sender_id,
                    text=msg.text,
                    reply_fn=_reply,
                    display_name=msg.metadata.get("display_name", ""),
                    account_id=msg.account_id,
                )
            )
            if not pushed:
                logger.warning(
                    "channel_inject: queue full, dropping message from %s/%s",
                    platform,
                    sender_id,
                )
            return

        # Run agent — select per-platform runner (chat or kairos mode)
        runner = self._get_runner_for(platform)
        try:
            response = await runner.run_turn(
                msg.text,
                session_id=conv_key,
                history=list(state.history[:-1]),  # exclude the turn we just appended
                system_prompt=self._config.system_prompt,
                max_turns=self._config.max_turns,
            )
        except Exception:
            logger.exception(
                "Agent run failed for conv_key=%s platform=%s sender=%s",
                conv_key,
                platform,
                sender_id,
            )
            response = "Sorry, I encountered an error processing your message."

        # Persist assistant reply
        self._store.append_assistant_message(
            conv_key,
            response,
            max_history_entries=self._config.max_history_entries,
        )

        # Send reply
        if adapter:
            reply_to = msg.metadata.get("chat_id", sender_id)
            ok = await adapter.send(str(reply_to), response)
            if not ok:
                logger.warning(
                    "Failed to send reply on platform=%s to=%s", platform, reply_to
                )
        else:
            logger.warning(
                "No adapter registered for platform=%s, response dropped", platform
            )
