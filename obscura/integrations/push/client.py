"""Push notification client supporting APNs (HTTP/2) and FCM (v1 API) and Expo."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

logger = logging.getLogger(__name__)

Provider = Literal["apns", "fcm", "expo"]


@dataclass(frozen=True)
class PushReceipt:
    """Result of a push notification delivery attempt."""

    notification_id: str
    provider: Provider
    token: str
    success: bool
    status_code: int = 0
    error: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


class PushClient:
    """Send push notifications via APNs, FCM, or Expo Push Service."""

    def __init__(
        self,
        tokens: list[str],
        *,
        provider: Provider = "expo",
        apns_key_id: str | None = None,
        apns_team_id: str | None = None,
        apns_bundle_id: str | None = None,
        apns_key_path: str | None = None,
        fcm_server_key: str | None = None,
        fcm_project_id: str | None = None,
    ) -> None:
        self._tokens = tokens
        self._provider: Provider = provider
        self._apns_key_id = apns_key_id or os.environ.get("APNS_KEY_ID", "")
        self._apns_team_id = apns_team_id or os.environ.get("APNS_TEAM_ID", "")
        self._apns_bundle_id = apns_bundle_id or os.environ.get("APNS_BUNDLE_ID", "")
        self._apns_key_path = apns_key_path or os.environ.get("APNS_KEY_PATH", "")
        self._fcm_server_key = fcm_server_key or os.environ.get("FCM_SERVER_KEY", "")
        self._fcm_project_id = fcm_project_id or os.environ.get("FCM_PROJECT_ID", "")

    async def check_access(self) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._check_access_sync)

    def _check_access_sync(self) -> bool:
        if self._provider == "apns" and not self._apns_key_id:
            msg = "APNS_KEY_ID must be set for APNs push notifications."
            raise RuntimeError(msg)
        if self._provider == "fcm" and not self._fcm_server_key:
            msg = "FCM_SERVER_KEY must be set for FCM push notifications."
            raise RuntimeError(msg)
        return True

    async def poll_inbound(self, since_epoch_s: float) -> list[Any]:
        """Push is outbound-only; returns empty list."""
        return []

    async def send(
        self,
        token: str,
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
    ) -> PushReceipt:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self._send_sync,
            token,
            title,
            body,
            data or {},
        )

    def _send_sync(
        self,
        token: str,
        title: str,
        body_text: str,
        data: dict[str, Any],
    ) -> PushReceipt:
        notification_id = hashlib.sha1(
            f"{token}|{title}|{body_text}|{time.time()}".encode(),
        ).hexdigest()[:16]
        try:
            if self._provider == "expo":
                return self._send_expo(notification_id, token, title, body_text, data)
            if self._provider == "fcm":
                return self._send_fcm(notification_id, token, title, body_text, data)
            if self._provider == "apns":
                return self._send_apns(notification_id, token, title, body_text, data)
            msg = f"Unknown push provider: {self._provider}"
            raise ValueError(msg)
        except Exception as exc:
            logger.exception("Push send failed to token %s...", token[:10])
            return PushReceipt(
                notification_id=notification_id,
                provider=self._provider,
                token=token,
                success=False,
                error=str(exc),
            )

    def _send_expo(
        self,
        nid: str,
        token: str,
        title: str,
        body: str,
        data: dict[str, Any],
    ) -> PushReceipt:
        payload = json.dumps(
            {"to": token, "title": title, "body": body, "data": data},
        ).encode("utf-8")
        req = urllib.request.Request(
            "https://exp.host/--/api/v2/push/send",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            status_code = resp.status
            resp_data = json.loads(resp.read().decode("utf-8"))
        ticket = resp_data.get("data", {})
        success = ticket.get("status") == "ok"
        return PushReceipt(
            notification_id=nid,
            provider="expo",
            token=token,
            success=success,
            status_code=status_code,
            error=ticket.get("message", "") if not success else "",
        )

    def _send_fcm(
        self,
        nid: str,
        token: str,
        title: str,
        body: str,
        data: dict[str, Any],
    ) -> PushReceipt:
        payload = json.dumps(
            {
                "to": token,
                "notification": {"title": title, "body": body},
                "data": data,
            },
        ).encode("utf-8")
        req = urllib.request.Request(
            "https://fcm.googleapis.com/fcm/send",
            data=payload,
            headers={
                "Authorization": f"key={self._fcm_server_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            status_code = resp.status
            resp_data = json.loads(resp.read().decode("utf-8"))
        success = resp_data.get("success", 0) == 1
        error_msg = ""
        if not success:
            results = resp_data.get("results", [{}])
            error_msg = str(results[0].get("error", "")) if results else ""
        return PushReceipt(
            notification_id=nid,
            provider="fcm",
            token=token,
            success=success,
            status_code=status_code,
            error=error_msg,
        )

    def _send_apns(
        self,
        nid: str,
        token: str,
        title: str,
        body: str,
        data: dict[str, Any],
    ) -> PushReceipt:
        try:
            import apns2.client as _apns_client_mod  # type: ignore[import-untyped]
            import apns2.payload as _apns_payload_mod  # type: ignore[import-untyped]

            apns_client: Any = _apns_client_mod
            apns_payload: Any = _apns_payload_mod
            client: Any = apns_client.APNsClient(
                credentials=self._apns_key_path, use_sandbox=False
            )
            payload: Any = apns_payload.Payload(
                alert={"title": title, "body": body}, custom=data
            )
            notif: Any = apns_client.Notification(token=token, payload=payload)
            client.send_notification(notif, self._apns_bundle_id)
            return PushReceipt(
                notification_id=nid,
                provider="apns",
                token=token,
                success=True,
            )
        except ImportError as e:
            msg = "apns2 package required for APNs. Install: pip install apns2"
            raise RuntimeError(
                msg,
            ) from e
