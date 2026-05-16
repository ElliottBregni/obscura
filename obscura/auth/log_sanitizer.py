"""obscura.auth.log_sanitizer -- Strip sensitive headers/values from logs.

Auth tokens (``Authorization``, ``X-API-Key``, ``X-GitHub-Token``,
``apikey``) can leak through proxy access logs, telemetry middleware, and
error-reporting pipelines if applications naively serialize request
headers. This module provides two things:

1. :func:`sanitize_headers` — hand it a mapping, get a copy with known
   secret-bearing header names redacted to ``***REDACTED***``.
2. :class:`SensitiveHeaderFilter` — a logging filter that runs the same
   redaction across ``record.msg`` and the arg tuple so log lines built
   via ``logger.info("headers=%s", headers)`` are also scrubbed.

The intent is defense-in-depth, not a replacement for not-logging-in-the-
first-place.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any, cast, override

_SENSITIVE_HEADER_NAMES: frozenset[str] = frozenset(
    {
        "authorization",
        "x-api-key",
        "x-github-token",
        "apikey",
        "cookie",
        "set-cookie",
        "proxy-authorization",
    },
)

REDACTED = "***REDACTED***"

# Match bearer tokens + long opaque tokens embedded in arbitrary strings.
_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=\-]{8,}")
_GITHUB_TOKEN_RE = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,})")
_SUPABASE_PAT_RE = re.compile(r"\b(sbp_[A-Za-z0-9]{16,})")

# Additional patterns for channel/platform credential field values
_CRED_FIELD_PATTERNS = [
    # key: value in JSON/log output
    re.compile(r'("bot_token"\s*:\s*)"[^"]{8,}"', re.IGNORECASE),
    re.compile(r'("auth_token"\s*:\s*)"[^"]{8,}"', re.IGNORECASE),
    re.compile(r'("app_secret"\s*:\s*)"[^"]{8,}"', re.IGNORECASE),
    re.compile(r'("webhook_secret"\s*:\s*)"[^"]{8,}"', re.IGNORECASE),
    re.compile(r'("api_key"\s*:\s*)"[^"]{8,}"', re.IGNORECASE),
    re.compile(r'("phone_number_id"\s*:\s*)"[^"]{6,}"', re.IGNORECASE),
    re.compile(r'("account_sid"\s*:\s*)"[^"]{8,}"', re.IGNORECASE),
]


def sanitize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of *headers* with sensitive values replaced by a sentinel."""
    cleaned: dict[str, str] = {}
    for key, value in headers.items():
        cleaned[key] = REDACTED if key.lower() in _SENSITIVE_HEADER_NAMES else value
    return cleaned


def sanitize_text(text: str) -> str:
    """Redact bearer tokens, GitHub tokens, Supabase PATs anywhere in *text*."""
    if not text:
        return text
    text = _BEARER_RE.sub(r"\1" + REDACTED, text)
    text = _GITHUB_TOKEN_RE.sub(REDACTED, text)
    text = _SUPABASE_PAT_RE.sub(REDACTED, text)
    for pat in _CRED_FIELD_PATTERNS:
        text = pat.sub(r'\1"<redacted>"', text)
    return text


class SensitiveHeaderFilter(logging.Filter):
    """Logging filter that scrubs known secret patterns from formatted records.

    Attach once at startup::

        import logging
        from obscura.auth.log_sanitizer import SensitiveHeaderFilter

        logging.getLogger().addFilter(SensitiveHeaderFilter())

    The filter runs on ``record.msg`` and ``record.args`` — it won't catch
    messages already rendered into ``record.message`` (rare — only happens
    when other filters pre-render), and by design it's best-effort:
    authoritative fix is to not log secrets in the first place.
    """

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = sanitize_text(record.msg)
        if record.args:
            record.args = _sanitize_args(record.args)
        return True


def _sanitize_args(args: Any) -> Any:
    if isinstance(args, tuple):
        tup = cast(tuple[Any, ...], args)
        return tuple(_sanitize_one(a) for a in tup)
    if isinstance(args, dict):
        d = cast(dict[Any, Any], args)
        return {k: _sanitize_one(v) for k, v in d.items()}
    return _sanitize_one(args)


def _sanitize_one(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, Mapping):
        m = cast(Mapping[Any, Any], value)
        return {k: _sanitize_one(v) for k, v in m.items()}
    return value


__all__ = [
    "REDACTED",
    "SensitiveHeaderFilter",
    "sanitize_headers",
    "sanitize_text",
]
