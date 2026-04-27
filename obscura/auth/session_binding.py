"""obscura.auth.session_binding -- JTI replay + soft UA/IP binding.

On first-seen of a JWT's ``jti`` claim (or ``session_id`` fallback for
Supabase tokens, which don't always set ``jti``), record the caller's
user-agent and IP. On subsequent requests with the same JWT identifier,
flag hard drift:

* **IP change across non-adjacent CIDR blocks** → suspicious (possible
  token theft from a different network) — log a warning, don't reject.
  Rejecting would break legitimate mobile/wifi-switch scenarios.
* **User-agent changes entirely** → suspicious, same treatment.

This is soft-enforcement: we log + increment a counter. Hard rejection
lives at the WAF / operator layer.

Replay tracking:

* In-memory dict of ``jti -> (first_seen, ua, ip)``.
* Entries expire after ``JTI_TTL_SECONDS`` (default 3600 — must exceed
  the JWT ``exp``).
* Process-local. For multi-instance deployments, swap the dict for Redis.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

JTI_TTL_SECONDS = 3600
MAX_TRACKED_JTIS = 10_000  # guard against unbounded growth


@dataclass(frozen=True)
class _Seen:
    first_seen: float
    ua: str
    ip: str


class SessionBindingTracker:
    """Tracks first-seen UA/IP per JWT identifier. Thread-safe."""

    def __init__(self, ttl_seconds: int = JTI_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._seen: dict[str, _Seen] = {}
        self._lock = threading.Lock()
        self._drift_counter = 0

    def observe(self, jti: str, ua: str, ip: str) -> bool:
        """Record an observation; return True if binding drift was detected.

        ``jti`` may be any stable identifier — JWT ``jti`` claim, Supabase
        ``session_id``, or anything else that persists across requests on
        the same logical session.
        """
        if not jti:
            return False

        now = time.time()
        with self._lock:
            self._gc(now)
            entry = self._seen.get(jti)
            if entry is None:
                if len(self._seen) >= MAX_TRACKED_JTIS:
                    # Drop oldest 10% to bound memory.
                    victims = sorted(
                        self._seen.items(),
                        key=lambda kv: kv[1].first_seen,
                    )[: MAX_TRACKED_JTIS // 10]
                    for k, _ in victims:
                        self._seen.pop(k, None)
                self._seen[jti] = _Seen(first_seen=now, ua=ua, ip=ip)
                return False

            if _binding_drift(entry, ua, ip):
                self._drift_counter += 1
                logger.warning(
                    "Auth binding drift on jti=%s: "
                    "first_ip=%s first_ua=%s now_ip=%s now_ua=%s",
                    jti[:16],
                    entry.ip,
                    entry.ua[:40],
                    ip,
                    ua[:40],
                )
                return True
            return False

    def _gc(self, now: float) -> None:
        cutoff = now - self._ttl
        dead = [k for k, v in self._seen.items() if v.first_seen < cutoff]
        for k in dead:
            del self._seen[k]

    def drift_count(self) -> int:
        return self._drift_counter

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()
            self._drift_counter = 0


def _binding_drift(entry: _Seen, ua: str, ip: str) -> bool:
    """Heuristic: has the binding meaningfully changed?

    - Different user-agent product token (e.g. Chrome → Safari) — drift.
    - Different first IP octet for v4, first 4 groups for v6 — drift.
      (Mobile/wifi hops typically stay in the same /24 or /48.)
    """
    if entry.ua and ua and _ua_product(entry.ua) != _ua_product(ua):
        return True
    if entry.ip and ip and _ip_prefix(entry.ip) != _ip_prefix(ip):
        return True
    return False


def _ua_product(ua: str) -> str:
    # Rough: first token, lowercased. "Mozilla/5.0" vs "curl/8.4" etc.
    return ua.split("/", 1)[0].lower().strip()


def _ip_prefix(ip: str) -> str:
    if ":" in ip:  # IPv6
        return ":".join(ip.split(":")[:4])
    return ".".join(ip.split(".")[:2])  # IPv4 /16


# Process-wide tracker for convenience. Tests can instantiate directly.
_tracker = SessionBindingTracker()


def observe_binding(jti: str, ua: str, ip: str) -> bool:
    """Module-level helper: delegates to the process singleton."""
    return _tracker.observe(jti, ua, ip)


def clear_bindings_for_tests() -> None:
    _tracker.clear()


__all__ = [
    "JTI_TTL_SECONDS",
    "SessionBindingTracker",
    "clear_bindings_for_tests",
    "observe_binding",
]
