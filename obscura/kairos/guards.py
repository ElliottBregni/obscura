from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast
import logging

logger = logging.getLogger(__name__)


def pre_tool_use_guard(context: Mapping[str, Any]) -> tuple[bool, str]:
    """Guard that decides whether a tool call from a given context should be
    allowed.

    Context (duck-typed) should include:
      - 'initiator': 'user' | 'kairos' | 'agent' | 'background' | 'daemon'
      - 'session': mapping with optional 'settings' dict containing 'kairos_enabled'

    Returns (allowed, reason).
    """
    initiator = str(context.get("initiator", "user")).lower()
    session_raw: Any = context.get("session") or {}

    # Resolve kairos enabled flag from common shapes
    kairos_enabled = False
    try:
        if isinstance(session_raw, Mapping):
            session_map = cast(Mapping[str, Any], session_raw)
            settings_raw: Any = (
                session_map.get("settings") or session_map.get("config") or {}
            )
            if isinstance(settings_raw, Mapping):
                settings = cast(Mapping[str, Any], settings_raw)
                kairos_section = settings.get("kairos")
                kairos_dict = (
                    cast(Mapping[str, Any], kairos_section)
                    if isinstance(kairos_section, Mapping)
                    else cast(Mapping[str, Any], {})
                )
                kairos_enabled = bool(
                    settings.get("kairos_enabled") or kairos_dict.get("enabled")
                )
    except Exception:
        logger.debug("suppressed exception in pre_tool_use_guard", exc_info=True)
        kairos_enabled = False

    # Background-originated calls are vetoed unless kairos_enabled is truthy
    if initiator in ("kairos", "background", "daemon") and not kairos_enabled:
        return (
            False,
            "vetoed: background-initiated tool calls require per-session opt-in",
        )

    return True, "allowed"
