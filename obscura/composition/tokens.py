"""obscura.composition.tokens — capability token generation.

Extracted from ``ObscuraClient.__init__`` so the composition layer can
build identity-scoped capability tokens without going through the
client. The token carries the user's tier (public / privileged / etc.)
and is consumed by the agent loop's capability gate.

Used in two places today:
- ``ObscuraClient.__init__`` (legacy path, still calls this helper)
- ``composition/core.py::build_core_session`` (composition path — passes
  the result onto ``AgentSession.capability_token``)
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from obscura.auth.capability import CapabilityTier

logger = logging.getLogger(__name__)


def generate_identity_token(
    user: Any,
    session_id: str | None = None,
) -> Any | None:
    """Generate a capability token for the given user.

    Returns ``None`` when ``user`` is None or not an
    :class:`AuthenticatedUser`. Soft-suppresses any failure (matches the
    legacy try/except pattern in ObscuraClient.__init__).
    """
    if user is None:
        return None
    try:
        from obscura.auth.capability import generate_capability_token
        from obscura.auth.models import AuthenticatedUser as _AuthUser

        if not isinstance(user, _AuthUser):
            return None
        sid = session_id or uuid.uuid4().hex
        return generate_capability_token(user, sid)
    except Exception:
        logger.debug("generate_identity_token: failed", exc_info=True)
        return None


def maybe_inject_tier_prompt(
    capability_token: Any | None,
    base_prompt: str,
) -> str:
    """Prepend the tier-specific system prompt onto ``base_prompt`` when
    ``inject_tier_prompt`` was requested.

    The tier prompt encodes whether the user is public/privileged/admin
    and is what the agent loop's capability gate compares against tool
    ``required_tier`` declarations.

    Returns ``base_prompt`` unchanged when there's no token or the
    tier_system_prompts module isn't available.
    """
    if capability_token is None:
        return base_prompt
    try:
        from obscura.auth.system_prompts import get_tier_system_prompt

        tier: CapabilityTier = capability_token.tier
        return get_tier_system_prompt(tier, additional=base_prompt)
    except Exception:
        logger.debug("maybe_inject_tier_prompt: failed", exc_info=True)
        return base_prompt
