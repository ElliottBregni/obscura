"""HTTP webhook client — signed fire-and-forget POST delivery."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 10
_RETRY_BACKOFF_S: tuple[float, ...] = (1.0, 2.0, 4.0)


@dataclass(frozen=True)
class WebhookDelivery:
    """Record of a webhook POST attempt."""

    delivery_id: str
    url: str
    payload: dict[str, Any]
    status_code: int
    success: bool
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


class WebhookClient:
    """Send signed JSON payloads to one or more webhook URLs."""

    def __init__(
        self,
        urls: list[str],
        *,
        secret: str | None = None,
        timeout_s: int = _DEFAULT_TIMEOUT_S,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._urls = urls
        self._secret = secret
        self._timeout_s = timeout_s
        self._extra_headers = headers or {}

    def _sign(self, body: bytes) -> str:
        if not self._secret:
            return ""
        return hmac.new(
            self._secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()

    async def check_access(self) -> bool:
        if not self._urls:
            msg = "No webhook URLs configured."
            raise RuntimeError(msg)
        return True

    async def deliver(
        self,
        recipient: str,
        text: str,
        extra: dict[str, Any] | None = None,
    ) -> WebhookDelivery:
        loop = asyncio.get_event_loop()
        targets = [recipient] if recipient else self._urls
        url = targets[0] if targets else ""
        return await loop.run_in_executor(
            None,
            self._deliver_sync,
            url,
            text,
            extra or {},
        )

    def _deliver_sync(
        self,
        url: str,
        text: str,
        extra: dict[str, Any],
    ) -> WebhookDelivery:
        delivery_id = hashlib.sha1(
            f"{url}|{text}|{time.time()}".encode(),
        ).hexdigest()[:16]
        payload: dict[str, Any] = {
            "event": "message",
            "text": text,
            "timestamp": datetime.now(tz=UTC).isoformat(),
            **extra,
        }
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        sig = self._sign(body)
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "Obscura-Webhook/1.0",
            **self._extra_headers,
        }
        if sig:
            headers["X-Obscura-Signature"] = f"sha256={sig}"

        status_code = 0
        backoffs = (*_RETRY_BACKOFF_S, None)
        for attempt, backoff in enumerate(backoffs):
            try:
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                    status_code = resp.status
                if 200 <= status_code < 300:
                    return WebhookDelivery(
                        delivery_id=delivery_id,
                        url=url,
                        payload=payload,
                        status_code=status_code,
                        success=True,
                    )
            except Exception as exc:
                logger.warning(
                    "Webhook delivery attempt %d failed: %s",
                    attempt + 1,
                    exc,
                )
            if backoff is not None:
                time.sleep(backoff)
        return WebhookDelivery(
            delivery_id=delivery_id,
            url=url,
            payload=payload,
            status_code=status_code,
            success=False,
        )
