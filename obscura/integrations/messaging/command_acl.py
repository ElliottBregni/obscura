"""Sender-based access control for REPL commands sent via messaging channels.

When a message arrives via WhatsApp (or any messaging platform) and
starts with a command prefix (``/``, ``$``, ``@``), the REPL consults
this module to decide whether the sender is allowed to execute that
command. If not allowed, the message is routed as plain text to the
agent (the existing default behavior) instead of as a command.

**Default-deny semantics**: an empty or missing allowlist denies
everyone — REPL commands via messaging are off unless the user
explicitly opts in by listing trusted sender numbers. Plain-text
conversation is unaffected.

Allowlist source
----------------
``[messaging.<platform>].command_allowlist`` in ``~/.obscura/config.toml``::

    [messaging.whatsapp]
    enabled = true
    transport = "wuzapi"
    command_allowlist = ["2316333624"]    # only this number can run /commands

Numbers are matched after digit-normalization (any non-digit stripped,
US country code ``1`` stripped if length ≥ 11). So ``"2316333624"``,
``"12316333624"``, ``"+1 (231) 633-3624"``, and the JID
``"12316333624@s.whatsapp.net"`` all match the same allowlist entry.
"""

from __future__ import annotations

import logging
import tomllib
from typing import Any, cast

from obscura.core.paths import resolve_obscura_home

logger = logging.getLogger(__name__)


def _normalize_digits(s: str) -> str:
    """Extract a digit string, dropping any country-code ``1`` prefix.

    Returns ``""`` if no digits are present. Examples::

        "12316333624@s.whatsapp.net" → "2316333624"
        "+1 (231) 633-3624"          → "2316333624"
        "alice@lid"                  → ""
    """
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) >= 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _read_allowlist(platform: str) -> list[str]:
    """Return ``[messaging.<platform>].command_allowlist`` from config.toml.

    Returns an empty list on any error (missing file, malformed TOML,
    wrong type for the field). Default-deny is the safe behavior.
    """
    cfg_path = resolve_obscura_home() / "config.toml"
    if not cfg_path.is_file():
        return []
    try:
        with cfg_path.open("rb") as fh:
            raw: dict[str, Any] = tomllib.load(fh)
    except Exception:
        logger.debug("command_acl: failed to read config.toml", exc_info=True)
        return []
    messaging_raw = raw.get("messaging", {})
    if not isinstance(messaging_raw, dict):
        return []
    messaging: dict[str, Any] = cast("dict[str, Any]", messaging_raw)
    section_raw = messaging.get(platform, {})
    if not isinstance(section_raw, dict):
        return []
    section: dict[str, Any] = cast("dict[str, Any]", section_raw)
    allowlist_raw = section.get("command_allowlist", [])
    if not isinstance(allowlist_raw, list):
        return []
    allowlist_any: list[Any] = cast("list[Any]", allowlist_raw)
    return [e for e in allowlist_any if isinstance(e, str)]


def is_command_allowed(platform: str, sender_id: str) -> bool:
    """Is ``sender_id`` permitted to run REPL commands over ``platform``?

    Default-deny: an empty/missing allowlist denies everyone. Returns
    True only when the normalized sender matches a normalized allowlist
    entry.
    """
    sender_norm = _normalize_digits(sender_id)
    if not sender_norm:
        return False
    for entry in _read_allowlist(platform):
        if _normalize_digits(entry) == sender_norm:
            return True
    return False


__all__ = ["is_command_allowed"]
