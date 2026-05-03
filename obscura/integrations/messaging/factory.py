"""Adapter registry/factory for message platforms."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

AdapterBuilder = Callable[..., Any]

logger = logging.getLogger(__name__)


def _build_imessage_adapter(*, contacts: list[str], account_id: str = "default") -> Any:
    from obscura.integrations.imessage import IMessageAdapter

    return IMessageAdapter(contacts, account_id=account_id)


def _build_whatsapp_adapter(*, contacts: list[str], account_id: str = "default") -> Any:
    from obscura.integrations.whatsapp import WhatsAppAdapter

    return WhatsAppAdapter(contacts, account_id=account_id)


def _build_signal_adapter(*, contacts: list[str], account_id: str = "default") -> Any:
    from obscura.integrations.signal import SignalAdapter

    return SignalAdapter(contacts, account_id=account_id)


def _build_slack_adapter(*, contacts: list[str], account_id: str = "default") -> Any:
    from obscura.integrations.slack import SlackAdapter

    return SlackAdapter(contacts, account_id=account_id)


def _build_webhook_adapter(*, contacts: list[str], account_id: str = "default") -> Any:
    from obscura.integrations.webhook import WebhookAdapter

    return WebhookAdapter(contacts, account_id=account_id)


def _build_push_adapter(*, contacts: list[str], account_id: str = "default") -> Any:
    from obscura.integrations.push import PushAdapter

    return PushAdapter(contacts, account_id=account_id)


def _build_telegram_adapter(*, contacts: list[str], account_id: str = "default") -> Any:
    from obscura.integrations.telegram import TelegramAdapter

    return TelegramAdapter(contacts, account_id=account_id)


_ADAPTER_BUILDERS: dict[str, AdapterBuilder] = {
    "imessage": _build_imessage_adapter,
    "whatsapp": _build_whatsapp_adapter,
    "signal": _build_signal_adapter,
    "slack": _build_slack_adapter,
    "webhook": _build_webhook_adapter,
    "push": _build_push_adapter,
    "telegram": _build_telegram_adapter,
}


def register_adapter(platform: str, builder: AdapterBuilder) -> None:
    """Register a platform adapter builder at runtime."""
    _ADAPTER_BUILDERS[platform.strip().lower()] = builder


def get_adapter(
    *,
    platform: str,
    contacts: list[str],
    account_id: str = "default",
) -> Any:
    """Construct an adapter for the requested platform."""
    key = platform.strip().lower()
    builder = _ADAPTER_BUILDERS.get(key)
    if builder is None:
        msg = (
            f"Unknown messaging platform '{platform}'. "
            f"Registered: {', '.join(sorted(_ADAPTER_BUILDERS))}"
        )
        raise ValueError(
            msg,
        )
    return builder(contacts=contacts, account_id=account_id)


def _parse_mode(value: str) -> str:
    """Normalise a mode string to 'chat' or 'kairos'."""
    v = value.strip().lower()
    if v in ("kairos", "k"):
        return "kairos"
    return "chat"


async def build_channel_router(
    *,
    system_prompt: str = (
        "You are a helpful assistant. Be concise. You are replying via a messaging app."
    ),
    backend_name: str | None = None,
    default_mode: str = "chat",
    platform_modes: dict[str, str] | None = None,
) -> Any:
    """Build a fully-wired ChannelRouter from environment variables.

    Detects and registers adapters for any platform whose credentials exist:

    * **Telegram** — ``TELEGRAM_BOT_TOKEN`` (required)
      Optional: ``TELEGRAM_ALLOWED_USERS`` (comma-separated user IDs to whitelist)
    * **WhatsApp (Twilio)** — ``TWILIO_ACCOUNT_SID`` + ``TWILIO_AUTH_TOKEN``
      Optional: ``TWILIO_WHATSAPP_FROM``, ``WHATSAPP_ALLOWED_NUMBERS``

    The LLM backend is chosen from ``OBSCURA_BACKEND`` env var (default: ``claude``).

    Each platform can run in either ``chat`` mode (fast single-turn AgentLoop) or
    ``kairos`` mode (long-horizon durable goal runtime).  Mode is resolved in this
    priority order:

    1. ``platform_modes`` kwarg (e.g. ``{"telegram": "kairos"}``)
    2. Per-platform env var: ``OBSCURA_TELEGRAM_MODE``, ``OBSCURA_WHATSAPP_MODE``
    3. ``default_mode`` kwarg
    4. ``OBSCURA_CHANNEL_MODE`` env var
    5. Falls back to ``chat``

    Returns a ready :class:`~obscura.integrations.messaging.router.ChannelRouter`.
    This coroutine must be ``await``-ed — it performs async backend initialisation.
    """
    from obscura.core.client import ObscuraClient
    from obscura.core.types import Backend
    from obscura.integrations.messaging.router import (
        ChannelMode,
        ChannelRouter,
        ChannelRouterConfig,
        ObscuraAgentRunner,
    )

    _backend_name = backend_name or os.environ.get("OBSCURA_BACKEND", "claude")
    try:
        _backend_enum = Backend(_backend_name)
    except ValueError:
        _backend_enum = Backend("claude")

    client = ObscuraClient(backend=_backend_enum)
    await client.start()

    backend_impl = client.backend_impl
    tool_registry = client._tool_registry  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

    # Resolve the global default mode (env var overrides kwarg default)
    _env_default = os.environ.get("OBSCURA_CHANNEL_MODE", "")
    resolved_default = _parse_mode(_env_default if _env_default else default_mode)

    # Build the always-present chat runner
    chat_runner = ObscuraAgentRunner(
        backend=backend_impl,
        tool_registry=tool_registry,
    )

    # Lazy-build a shared KAIROS runner (only if at least one platform needs it)
    _kairos_runner: Any = None

    def _get_kairos_runner() -> Any:
        nonlocal _kairos_runner
        if _kairos_runner is None:
            try:
                from obscura.core.paths import resolve_obscura_home
                from obscura.integrations.messaging.kairos_runner import (
                    KairosAgentRunner,
                    KairosRunnerConfig,
                )

                _kairos_runner = KairosAgentRunner(
                    backend=backend_impl,
                    tool_registry=tool_registry,
                    config=KairosRunnerConfig(
                        db_path=resolve_obscura_home() / "kairos.db",
                    ),
                )
                logger.info("KAIROS runner initialised for channel routing")
            except Exception:
                logger.warning(
                    "KAIROS runner failed to initialise; platform will use chat mode",
                    exc_info=True,
                )
                return None
        return _kairos_runner

    config = ChannelRouterConfig(system_prompt=system_prompt)
    channel_router: ChannelRouter = ChannelRouter(runner=chat_runner, config=config)

    def _apply_platform_mode(platform: str) -> None:
        """Resolve and apply the correct mode for a registered platform."""
        # 1. Explicit kwarg dict
        if platform_modes and platform in platform_modes:
            mode_str = _parse_mode(platform_modes[platform])
        else:
            # 2. Per-platform env var (e.g. OBSCURA_TELEGRAM_MODE)
            env_key = f"OBSCURA_{platform.upper()}_MODE"
            env_val = os.environ.get(env_key, "")
            mode_str = _parse_mode(env_val) if env_val else resolved_default

        if mode_str == "kairos":
            kr = _get_kairos_runner()
            if kr is not None:
                channel_router.set_platform_mode(
                    platform, ChannelMode.KAIROS, kairos_runner=kr
                )
                logger.info("Platform %s → KAIROS mode", platform)
                return
            logger.warning(
                "Platform %s requested KAIROS mode but runner unavailable; using chat",
                platform,
            )

        channel_router.set_platform_mode(platform, ChannelMode.CHAT)
        logger.info("Platform %s → chat mode", platform)

    # --- Telegram ---
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if telegram_token:
        try:
            from obscura.integrations.telegram.adapter import TelegramAdapter

            tg_raw = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
            tg_contacts = [c.strip() for c in tg_raw.split(",") if c.strip()]
            tg_adapter = TelegramAdapter(contacts=tg_contacts, bot_token=telegram_token)
            channel_router.register("telegram", tg_adapter)
            _apply_platform_mode("telegram")
        except Exception:
            logger.warning(
                "Telegram adapter failed to initialise; Telegram webhooks disabled",
                exc_info=True,
            )

    # --- WhatsApp (Twilio) ---
    wa_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    wa_token_env = os.environ.get("TWILIO_AUTH_TOKEN", "")
    wa_from = os.environ.get("TWILIO_WHATSAPP_FROM", "")
    if wa_sid and wa_token_env:
        try:
            from obscura.integrations.whatsapp.adapter import WhatsAppAdapter

            wa_raw = os.environ.get("WHATSAPP_ALLOWED_NUMBERS", "")
            wa_contacts = [c.strip() for c in wa_raw.split(",") if c.strip()]
            wa_adapter = WhatsAppAdapter(
                contacts=wa_contacts,
                account_sid=wa_sid,
                auth_token=wa_token_env,
                from_number=wa_from,
            )
            channel_router.register("whatsapp", wa_adapter)
            _apply_platform_mode("whatsapp")
        except Exception:
            logger.warning(
                "WhatsApp adapter failed to initialise; WhatsApp webhooks disabled",
                exc_info=True,
            )

    return channel_router
