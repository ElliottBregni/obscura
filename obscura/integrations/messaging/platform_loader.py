"""Load messaging platform configs from ~/.obscura/config.toml.

Config format::

    [messaging.whatsapp]
    enabled = true
    mode = "channel_inject"   # "chat" | "channel_inject" | "kairos"
    account_sid = "ACxxx"
    auth_token = "xxx"
    from_number = "+14155238886"
    app_secret = "meta-app-secret"       # webhook HMAC verification
    verify_token = "your-verify-token"   # Meta hub.verify_token
    contacts = ["+15551234567"]

    [messaging.telegram]
    enabled = true
    mode = "channel_inject"
    bot_token = "123456:ABC-DEF"
    webhook_secret = "optional"

    [messaging.imessage]
    enabled = true
    mode = "channel_inject"
    # no credentials needed — uses local AppleScript bridge
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from obscura.core.config_io import try_load_config

logger = logging.getLogger(__name__)

_KNOWN_PLATFORMS = {"whatsapp", "telegram", "imessage", "signal", "sms"}

# Keys that are not credentials — excluded from the credentials dict.
_META_KEYS = {"enabled", "mode", "contacts", "label"}


def load_messaging_platforms() -> list[dict[str, Any]]:
    """Return list of platform config dicts from config.toml.

    Each dict has keys: platform, enabled, mode, credentials, contacts, label.

    Also sets ``WHATSAPP_APP_SECRET`` / ``WHATSAPP_VERIFY_TOKEN`` /
    ``TELEGRAM_WEBHOOK_SECRET`` env vars so existing webhook route validators
    pick them up (only when the field is non-empty and the env var is not
    already set).

    Load order: ``~/.obscura/config.toml`` is the base; if a project-level
    ``.obscura/config.toml`` exists in the current working directory, its
    ``[messaging]`` section wins on a per-key basis (project overrides global).
    """
    global_cfg_path = Path.home() / ".obscura" / "config.toml"
    project_cfg_path = Path.cwd() / ".obscura" / "config.toml"

    global_cfg: dict[str, Any] = {}
    project_cfg: dict[str, Any] = {}

    raw_global = try_load_config(global_cfg_path)
    if raw_global is not None:
        global_cfg = raw_global

    raw_project = try_load_config(project_cfg_path)
    if raw_project is not None:
        project_cfg = raw_project

    # Merge messaging sections: project wins on same platform key.
    global_messaging: dict[str, Any] = global_cfg.get("messaging", {})
    project_messaging: dict[str, Any] = project_cfg.get("messaging", {})

    merged_messaging: dict[str, Any] = {**global_messaging, **project_messaging}

    if not merged_messaging:
        logger.debug("load_messaging_platforms: no [messaging] section found in config")
        return []

    results: list[dict[str, Any]] = []

    for platform_name, platform_raw in merged_messaging.items():
        if not isinstance(platform_raw, dict):
            logger.warning(
                "load_messaging_platforms: ignoring non-dict value for [messaging.%s]",
                platform_name,
            )
            continue

        platform = platform_name.lower().strip()

        if platform not in _KNOWN_PLATFORMS:
            logger.warning(
                "load_messaging_platforms: unknown platform '%s' — skipping "
                "(known: %s)",
                platform,
                ", ".join(sorted(_KNOWN_PLATFORMS)),
            )
            continue

        platform_cfg: dict[str, Any] = dict(platform_raw)

        enabled: bool = bool(platform_cfg.get("enabled", True))
        mode: str = str(platform_cfg.get("mode", "channel_inject"))
        contacts: list[str] = list(platform_cfg.get("contacts", []))
        label: str = str(platform_cfg.get("label", platform))

        # Build credentials from all keys except meta keys.
        credentials: dict[str, Any] = {
            k: v for k, v in platform_cfg.items() if k not in _META_KEYS
        }

        # Side-effects: set env vars that webhook route handlers read.
        if platform == "whatsapp":
            _maybe_set_env(
                "WHATSAPP_APP_SECRET",
                str(credentials.get("app_secret", "")),
            )
            _maybe_set_env(
                "WHATSAPP_VERIFY_TOKEN",
                str(credentials.get("verify_token", "")),
            )

        elif platform == "telegram":
            _maybe_set_env(
                "TELEGRAM_WEBHOOK_SECRET",
                str(credentials.get("webhook_secret", "")),
            )

        results.append(
            {
                "platform": platform,
                "enabled": enabled,
                "mode": mode,
                "credentials": credentials,
                "contacts": contacts,
                "label": label,
            }
        )

        logger.debug(
            "load_messaging_platforms: loaded platform=%s enabled=%s mode=%s",
            platform,
            enabled,
            mode,
        )

    logger.info(
        "load_messaging_platforms: loaded %d platform(s): %s",
        len(results),
        ", ".join(r["platform"] for r in results),
    )

    return results


def _maybe_set_env(var: str, value: str) -> None:
    """Set *var* to *value* only if *value* is non-empty and *var* is not already set."""
    if value and not os.environ.get(var):
        os.environ[var] = value
        logger.debug("load_messaging_platforms: set env var %s", var)


__all__ = ["load_messaging_platforms"]
