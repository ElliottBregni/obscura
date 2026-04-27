"""obscura.auth.security_headers -- Defense-in-depth HTTP response headers.

Adds the low-risk, broadly-compatible set of security headers recommended
by OWASP to every response. Tuned for an API server that also serves a
SPA bundle — CSP uses ``connect-src 'self' https://*.supabase.co`` and
does NOT set ``frame-ancestors 'none'`` because some routes legitimately
render SSE in iframes during e2e testing. Override via env:

* ``OBSCURA_SECURITY_HEADERS_DISABLE=1`` — skip adding any headers.
* ``OBSCURA_CSP_OVERRIDE=<policy>`` — replace the built-in CSP.
* ``OBSCURA_HSTS_MAX_AGE=31536000`` — HSTS lifetime (default 1y).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, override

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response


_DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https:; "
    "font-src 'self' data:; "
    "connect-src 'self' https://*.supabase.co wss://*.supabase.co; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach HSTS / CSP / COOP / XFO / etc. to every response."""

    def __init__(self, app: Any) -> None:
        super().__init__(app)

    @override
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        response = await call_next(request)
        if os.environ.get("OBSCURA_SECURITY_HEADERS_DISABLE", "").strip().lower() in (
            "1",
            "true",
        ):
            return response

        csp = os.environ.get("OBSCURA_CSP_OVERRIDE", _DEFAULT_CSP)
        hsts_max_age = os.environ.get("OBSCURA_HSTS_MAX_AGE", "31536000")

        # Only set missing headers so upstream middleware (e.g. custom CSP
        # for a specific route) can win.
        headers = response.headers
        headers.setdefault("Content-Security-Policy", csp)
        headers.setdefault(
            "Strict-Transport-Security",
            f"max-age={hsts_max_age}; includeSubDomains",
        )
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault(
            "Permissions-Policy",
            "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
            "magnetometer=(), microphone=(), payment=(), usb=()",
        )
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        # Don't leak server fingerprint. Starlette's MutableHeaders
        # supports `del` but not `pop`.
        if "Server" in headers:
            del headers["Server"]
        return response


__all__ = ["SecurityHeadersMiddleware"]
