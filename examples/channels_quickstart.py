"""Channels quickstart — Telegram + WhatsApp polling demo.

The fastest way to get Obscura talking back over Telegram or WhatsApp.
No open ports. No webhooks. Just export env vars and run.

TELEGRAM (5 minutes):
    1. Message @BotFather on Telegram → /newbot → copy token
    2. export TELEGRAM_BOT_TOKEN=your_token_here
    3. python examples/channels_quickstart.py --platform telegram

WHATSAPP via Twilio Sandbox (15 minutes):
    1. Sign up at https://www.twilio.com (free)
    2. Activate the WhatsApp Sandbox: console.twilio.com → Messaging → Try it out → Send a WhatsApp message
    3. Note your sandbox number (e.g. +14155238886) and join code
    4. export TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    5. export TWILIO_AUTH_TOKEN=your_auth_token
    6. export TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
    7. python examples/channels_quickstart.py --platform whatsapp

BOTH:
    python examples/channels_quickstart.py --platform telegram --platform whatsapp

CUSTOM SYSTEM PROMPT:
    python examples/channels_quickstart.py --platform telegram \\
        --system-prompt "You are a terse CLI assistant. Reply in one sentence."
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("channels_quickstart")

# Silence noisy libs
for noisy in ("httpx", "httpcore", "twilio", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


async def build_runner() -> object:
    """Build an ObscuraAgentRunner using the default backend from env."""
    from obscura.core.client import ObscuraClient
    from obscura.core.types import Backend
    from obscura.integrations.messaging.router import ObscuraAgentRunner

    backend_name = os.environ.get("OBSCURA_BACKEND", "claude")
    try:
        backend_enum = Backend(backend_name)
    except ValueError:
        print(
            f"Unknown backend '{backend_name}'. Set OBSCURA_BACKEND to: claude, copilot, openai, localllm"
        )
        sys.exit(1)

    logger.info("Initializing ObscuraClient (backend=%s) …", backend_name)
    client = ObscuraClient(backend=backend_enum)
    await client.init()
    logger.info("Client ready.")

    return ObscuraAgentRunner(
        backend=client.backend,
        tool_registry=client.tool_registry,
    )


def build_telegram_adapter() -> object | None:
    """Build a TelegramAdapter from env vars. Returns None if unconfigured."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram disabled.")
        return None

    from obscura.integrations.telegram import TelegramAdapter

    return TelegramAdapter(
        contacts=[],  # empty = accept from anyone
        bot_token=token,
        webhook_secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET"),
    )


def build_whatsapp_adapter() -> object | None:
    """Build a WhatsAppAdapter from env vars. Returns None if unconfigured."""
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    from_num = os.environ.get("TWILIO_WHATSAPP_FROM", "").strip()

    if not (sid and token and from_num):
        logger.warning(
            "WhatsApp requires TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN + TWILIO_WHATSAPP_FROM — disabled."
        )
        return None

    from obscura.integrations.whatsapp.adapter import WhatsAppAdapter

    return WhatsAppAdapter(
        contacts=[],  # empty = accept from anyone
        account_id="channel",
        account_sid=sid,
        auth_token=token,
        from_number=from_num,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main(platforms: list[str], system_prompt: str, poll_interval: float) -> None:
    from obscura.agent.channel_daemon import ChannelDaemon, ChannelDaemonConfig
    from obscura.integrations.messaging.router import ChannelRouter, ChannelRouterConfig

    runner = await build_runner()
    config = ChannelRouterConfig(
        system_prompt=system_prompt,
        max_turns=8,
        session_timeout_seconds=3600,
    )
    router = ChannelRouter(runner=runner, config=config)
    daemon = ChannelDaemon(
        router=router,
        config=ChannelDaemonConfig(poll_interval_seconds=poll_interval),
    )

    adapter_map = {
        "telegram": build_telegram_adapter,
        "whatsapp": build_whatsapp_adapter,
    }

    active = []
    for platform in platforms:
        builder = adapter_map.get(platform.lower())
        if builder is None:
            logger.warning("Unknown platform '%s' — skipping.", platform)
            continue
        adapter = builder()
        if adapter is not None:
            daemon.add_adapter(platform.lower(), adapter)
            active.append(platform.lower())

    if not active:
        print(
            "\n❌  No adapters configured. Check the env vars listed at the top of this file.\n"
        )
        sys.exit(1)

    print("\n🚀  Obscura channel daemon running")
    print(f"    Platforms : {', '.join(active)}")
    print(f"    Backend   : {os.environ.get('OBSCURA_BACKEND', 'claude')}")
    print(f"    Poll every: {poll_interval}s")
    print("    Ctrl-C    : stop\n")

    await daemon.run()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Obscura channels quickstart")
    parser.add_argument(
        "--platform",
        "-p",
        action="append",
        dest="platforms",
        default=[],
        choices=["telegram", "whatsapp"],
        help="Platform(s) to enable (repeat for multiple).",
    )
    parser.add_argument(
        "--system-prompt",
        default="You are a helpful assistant. Be concise. You are replying via a messaging app.",
        help="System prompt for the agent.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="Seconds between polls (default: 5).",
    )
    args = parser.parse_args()

    platforms = args.platforms or ["telegram", "whatsapp"]  # try both if none specified

    try:
        asyncio.run(main(platforms, args.system_prompt, args.poll_interval))
    except KeyboardInterrupt:
        print("\nStopped.")
