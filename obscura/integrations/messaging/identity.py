"""Identity normalization and conversation key helpers."""

from __future__ import annotations

import hashlib
import re


def normalize_identity(identity: str) -> str:
    """Normalize identities across transports for stable threading."""
    raw = (identity or "").strip()
    if not raw:
        return "unknown"

    lowered = raw.lower()
    for prefix in ("tel:", "sms:", "imessage:", "mailto:"):
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix):]
            break

    if "@" in lowered:
        return lowered

    has_plus = lowered.startswith("+")
    digits = re.sub(r"\D", "", lowered)
    if digits:
        return f"+{digits}" if has_plus else digits
    return lowered


def build_conversation_key(
    *,
    platform: str,
    account_id: str,
    channel_id: str,
    participants: list[str],
) -> str:
    """Build stable conversation key from normalized transport fields."""
    parts = sorted(normalize_identity(p) for p in participants)
    payload = "|".join(
        [platform.strip().lower(), account_id.strip().lower(), channel_id.strip().lower(), ",".join(parts)]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()
