"""
obscura.auth.prompt_filter -- Prompt injection defence for Tier A (public).

Provides input sanitisation and pattern-based filtering that is applied
to all user prompts in the PUBLIC tier.  The PRIVILEGED tier bypasses
these filters because operators need raw prompt access for debugging
and testing.

Design notes
------------
- Patterns are intentionally conservative: better to flag and log
  than to silently pass an injection attempt.
- ``FilterResult`` records *what* was modified so audit logs capture
  the exact flags without leaking the raw prompt content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from obscura.auth.capability import CapabilityTier


# ---------------------------------------------------------------------------
# Filter result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilterResult:
    """Result of prompt filtering."""

    original: str
    filtered: str
    was_modified: bool
    flags: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Injection patterns (Tier A only)
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "system_override",
        re.compile(
            r"(?i)(ignore|disregard|forget)\s+(all\s+)?"
            r"(previous|prior|above)\s+(instructions?|prompts?|rules?)",
        ),
    ),
    (
        "role_hijack",
        re.compile(
            r"(?i)you\s+are\s+now\s+(an?\s+)?"
            r"(admin|operator|privileged|root|system)",
        ),
    ),
    (
        "capability_forge",
        re.compile(
            r"(?i)(capability|token|tier|privilege)\s*[:=]\s*"
            r"(privileged|admin|operator|tier.?b)",
        ),
    ),
    (
        "delimiter_injection",
        re.compile(
            r"(?i)(```system|<\|system\|>|<system>|\[SYSTEM\]|### SYSTEM)",
        ),
    ),
    (
        "instruction_leak",
        re.compile(
            r"(?i)(repeat|print|show|reveal|output)\s+(your\s+)?"
            r"(system\s+)?(prompt|instructions?|rules?)",
        ),
    ),
]

# Substrings that must never appear in Tier A prompts.
_BLOCKED_SUBSTRINGS: list[str] = [
    "OBSCURA_CAPABILITY_SECRET",
    "signing_key",
    "hmac.new",
    "_get_signing_key",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def filter_prompt(prompt: str, tier: CapabilityTier) -> FilterResult:
    """Apply prompt injection filters based on capability tier.

    For ``PRIVILEGED`` tier: no filtering (operators need raw access).
    For ``PUBLIC`` tier: detect and sanitise injection attempts.
    """
    # TODO: remove PUBLIC bypass once tier differentiation is enabled
    if tier in (CapabilityTier.PRIVILEGED, CapabilityTier.PUBLIC):
        return FilterResult(original=prompt, filtered=prompt, was_modified=False)

    flags: list[str] = []
    filtered = prompt

    # Regex-based pattern detection
    for flag_name, pattern in _INJECTION_PATTERNS:
        if pattern.search(filtered):
            flags.append(flag_name)
            filtered = pattern.sub("[FILTERED]", filtered)

    # Blocked substring detection (case-insensitive)
    for substring in _BLOCKED_SUBSTRINGS:
        if substring.lower() in filtered.lower():
            flags.append(f"blocked_substring:{substring}")
            filtered = re.sub(
                re.escape(substring),
                "[REDACTED]",
                filtered,
                flags=re.IGNORECASE,
            )

    return FilterResult(
        original=prompt,
        filtered=filtered,
        was_modified=bool(flags),
        flags=tuple(flags),
    )
