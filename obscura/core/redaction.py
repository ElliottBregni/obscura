"""Centralised redaction for Obscura logs, audit events, and tool I/O.

This module exists so we have exactly one place where the "what counts as
a secret" policy lives. Every log/audit/event path should flow through
``redact_text`` or ``redact_mapping``; specific pattern additions go in
``_STRICT_PATTERNS`` / ``_STANDARD_PATTERNS`` below.

The policy is a SOC2 CC2 and C1 control — we deliberately redact by
default and require an explicit opt-out via ``OBSCURA_REDACTION_LEVEL=off``
(which logs a warning at startup when honoured).

Three levels:

- ``strict``  — aggressive. Redacts even low-confidence patterns (email,
  arbitrary long hex blobs) at the cost of extra false positives.
- ``standard`` — default. Redacts known credential formats with high
  confidence. Suitable for production logs.
- ``off``     — no redaction. For local development only, never for
  production. The config validator emits a warning when this is active
  in anything other than a loopback / no-auth local profile.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any

REDACTED = "[REDACTED]"

# Bump this threshold callers use to decide what to redact. If you're
# adding a new sink that handles user text, redact everything above
# ``CONFIDENTIAL`` by default.


class DataClassification(str, Enum):
    """Sensitivity tier for data flowing through Obscura.

    Used by call sites that have to decide whether to redact, encrypt,
    or restrict access. The string values are stable so they can be
    serialised into audit events without a version mapping.
    """

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class RedactionLevel(str, Enum):
    STRICT = "strict"
    STANDARD = "standard"
    OFF = "off"


# ---------------------------------------------------------------------------
# Pattern library
# ---------------------------------------------------------------------------

# Patterns match known credential formats precisely enough to avoid
# false positives in normal prose. Keep them anchored (word boundaries)
# where possible so they don't mid-match inside unrelated tokens.
_STANDARD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # GitHub tokens (ghp_, gho_, ghu_, ghs_, ghr_) — 36-255 chars of base62
    ("github-token", re.compile(r"\bgh[opusr]_[A-Za-z0-9]{36,255}\b")),
    # GitHub fine-grained PATs
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82,}\b")),
    # Anthropic API keys
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    # OpenAI keys (sk-... or sk-proj-...)
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    # AWS access key IDs
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # AWS secret access keys — 40 base64 chars is the defacto shape
    ("aws-secret-key", re.compile(r"\b[A-Za-z0-9/+=]{40}\b(?=\s|$|[\"'])")),
    # Google API keys
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    # Slack bot/user tokens
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    # Generic JWT shape (three base64url sections). Safe because JWTs are
    # almost never legitimate unredacted log content.
    (
        "jwt",
        re.compile(
            r"\bey[A-Za-z0-9_-]{10,}\.ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
        ),
    ),
    # Generic Authorization: Bearer <token> — the token portion only
    (
        "bearer-token",
        re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-]{16,})"),
    ),
    # PEM private key headers (we capture from header to footer)
    (
        "pem-private-key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
            r"[\s\S]+?-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
        ),
    ),
    # Obscura capability tokens (if introduced) — defensive placeholder
    ("obscura-capability", re.compile(r"\bobscap_[A-Za-z0-9_-]{20,}\b")),
)

_STRICT_EXTRA_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Email addresses — sometimes legitimate in logs, often not. Strict
    # mode treats them as PII to be redacted.
    (
        "email",
        re.compile(
            r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        ),
    ),
    # Opaque 32+ hex blobs (API keys from many vendors)
    ("hex-secret", re.compile(r"\b[A-Fa-f0-9]{32,}\b")),
)


# Keys whose VALUES should always be redacted when they appear in a
# mapping (e.g., an audit details dict). Case-insensitive exact match
# on the key name.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "api_key",
        "apikey",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "provider_token",
        "authorization",
        "cookie",
        "set-cookie",
        "client_secret",
        "private_key",
        "x-api-key",
        "x-github-token",
    }
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def current_level() -> RedactionLevel:
    """Resolve the redaction level from env with a conservative default."""
    raw = os.environ.get("OBSCURA_REDACTION_LEVEL", "").strip().lower()
    if raw == "off":
        return RedactionLevel.OFF
    if raw == "strict":
        return RedactionLevel.STRICT
    return RedactionLevel.STANDARD


def _patterns_for(level: RedactionLevel) -> Iterable[tuple[str, re.Pattern[str]]]:
    if level is RedactionLevel.OFF:
        return ()
    if level is RedactionLevel.STRICT:
        return _STANDARD_PATTERNS + _STRICT_EXTRA_PATTERNS
    return _STANDARD_PATTERNS


# Keep a tiny counter around for OTel metrics / diagnostics. Not a
# perfect counter (not thread-safe by design — we don't want locking on
# a hot path) but accurate enough for "did redaction fire at all today".
_hit_counts: dict[str, int] = {}


def hit_counts() -> dict[str, int]:
    """Return a snapshot of per-pattern redaction hit counts."""
    return dict(_hit_counts)


def reset_hit_counts() -> None:
    """Testing / observability helper."""
    _hit_counts.clear()


def redact_text(
    text: str,
    level: RedactionLevel | None = None,
) -> str:
    """Apply the configured redaction patterns to ``text``.

    Returns the text unchanged if no patterns match or level is OFF.
    """
    if not text:
        return text
    lvl = level if level is not None else current_level()
    if lvl is RedactionLevel.OFF:
        return text
    out = text
    for name, pattern in _patterns_for(lvl):
        new, count = pattern.subn(_replacement_for(name), out)
        if count:
            _hit_counts[name] = _hit_counts.get(name, 0) + count
            out = new
    return out


def _replacement_for(name: str) -> str:
    # The bearer-token regex has a capture group for the "Bearer " prefix;
    # we preserve that so logs stay readable ("Authorization: Bearer [REDACTED]").
    if name == "bearer-token":
        return r"\1" + REDACTED
    return REDACTED


def redact_mapping(
    mapping: Mapping[str, Any],
    level: RedactionLevel | None = None,
) -> dict[str, Any]:
    """Return a shallow copy of ``mapping`` with sensitive values redacted.

    - Values under well-known sensitive keys are replaced wholesale.
    - String values elsewhere are run through ``redact_text``.
    - Nested mappings are recursively redacted.
    - Lists are redacted element-wise for strings and mappings.
    """
    lvl = level if level is not None else current_level()
    if lvl is RedactionLevel.OFF:
        return dict(mapping)
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        key_str = str(key)
        if key_str.lower() in _SENSITIVE_KEYS:
            out[key_str] = REDACTED
            continue
        out[key_str] = _redact_value(value, lvl)
    return out


def _redact_value(value: Any, level: RedactionLevel) -> Any:
    if isinstance(value, str):
        return redact_text(value, level)
    if isinstance(value, Mapping):
        return redact_mapping(value, level)  # type: ignore[arg-type]
    if isinstance(value, list):
        return [_redact_value(item, level) for item in value]  # type: ignore[reportUnknownVariableType]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, level) for item in value)  # type: ignore[reportUnknownVariableType]
    return value


# ---------------------------------------------------------------------------
# Identity hashing
# ---------------------------------------------------------------------------

# Pepper for user-id hashing. Changing it invalidates existing hashed
# IDs in the audit log — treat as a versioned secret if we ever rotate.
_ID_HASH_PEPPER_ENV = "OBSCURA_ID_HASH_PEPPER"


def hash_identifier(value: str) -> str:
    """Return a stable, non-reversible hash of a user identifier.

    Used in audit logs instead of raw user_email. Short enough to be
    useful for grouping, long enough to make enumeration impractical.
    """
    if not value:
        return ""
    pepper = os.environ.get(_ID_HASH_PEPPER_ENV, "obscura-default-pepper")
    digest = hashlib.sha256((pepper + ":" + value).encode("utf-8")).hexdigest()
    return "u_" + digest[:16]
