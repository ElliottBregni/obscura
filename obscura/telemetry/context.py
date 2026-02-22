"""
obscura.telemetry.context — Identity-to-span helper.

Attaches user identity attributes from an :class:`AuthenticatedUser`
to the current OTel span. Handles the case where auth is not available
(CLI mode).

Usage::

    from obscura.telemetry.context import enrich_span_with_user

    enrich_span_with_user(span, request.state.user)
"""

from __future__ import annotations

from typing import Any


def enrich_span_with_user(span: Any, user: Any | None) -> None:
    """Attach user identity attributes to *span*.

    Parameters
    ----------
    span:
        An OTel ``Span`` instance (or any object with ``set_attribute``).
    user:
        An ``AuthenticatedUser`` dataclass, or ``None`` for CLI / unauthenticated
        contexts.
    """
    if user is None:
        span.set_attribute("user.id", "system")
        span.set_attribute("user.email", "system")
        span.set_attribute("user.auth_type", "none")
        return

    span.set_attribute("user.id", getattr(user, "user_id", "unknown"))
    span.set_attribute("user.email", getattr(user, "email", "unknown"))
    span.set_attribute("user.org_id", getattr(user, "org_id", "") or "")
    span.set_attribute("user.token_type", getattr(user, "token_type", "unknown"))

    roles = getattr(user, "roles", [])
    if roles:
        span.set_attribute("user.roles", ",".join(roles))


def get_user_id(user: Any | None) -> str:
    """Extract user_id from an AuthenticatedUser, or return 'system'."""
    if user is None:
        return "system"
    return getattr(user, "user_id", "system")


def get_user_email(user: Any | None) -> str:
    """Extract email from an AuthenticatedUser, or return 'system'."""
    if user is None:
        return "system"
    return getattr(user, "email", "system")
