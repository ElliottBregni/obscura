"""Low-level Signal client via signal-cli JSON-RPC REST bridge."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _str_any_dict() -> dict[str, Any]:
    return {}


_DEFAULT_BASE_URL = "http://localhost:8080"
_DEFAULT_TIMEOUT_S = 10

@dataclass(frozen=True)
class SignalMessage:
    """A single inbound Signal message envelope."""

    envelope_ts_ms: int
    sender_number: str
    sender_name: str
    recipient_number: str
    body: str
    timestamp: datetime
    group_id: str | None
    raw: dict[str, Any] = field(default_factory=_str_any_dict)


class SignalClient:
    """Interface to signal-cli via its JSON-RPC REST bridge.

    Requires signal-cli >=0.11 running in daemon mode:
        signal-cli -a +1234567890 daemon --http localhost:8080
    """

    def __init__(
        self,
        contacts: list[str],
        *,
        account_number: str | None = None,
        base_url: str | None = None,
        timeout_s: int = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._contacts = contacts
        self._account = account_number or os.environ.get("SIGNAL_ACCOUNT_NUMBER", "")
        self._base_url = (
            base_url or os.environ.get("SIGNAL_CLI_URL", _DEFAULT_BASE_URL)
        ).rstrip("/")
        self._timeout_s = timeout_s

    def _jsonrpc(self, method: str, params: dict[str, Any]) -> Any:
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base_url}/api/v1/rpc",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))

    async def check_access(self) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._check_access_sync)

    def _check_access_sync(self) -> bool:
        if not self._account:
            raise RuntimeError(
                "SIGNAL_ACCOUNT_NUMBER must be set for Signal integration. "
                "Also ensure signal-cli daemon is running."
            )
        try:
            resp = self._jsonrpc("listAccounts", {})
            if "error" in resp:
                raise RuntimeError(f"signal-cli error: {resp['error']}")
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Cannot reach signal-cli at {self._base_url}. "
                "Ensure signal-cli daemon is running."
            ) from e
        return True

    async def receive(self) -> list[SignalMessage]:
        """Receive pending messages from signal-cli daemon."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._receive_sync)

    def _receive_sync(self) -> list[SignalMessage]:
        resp = self._jsonrpc("receive", {"account": self._account, "timeout": 1})
        envelopes = resp.get("result", [])
        out: list[SignalMessage] = []
        for env in envelopes:
            data_message = env.get("envelope", {}).get("dataMessage", {})
            if not data_message:
                continue
            body = str(data_message.get("message", "") or "")
            if not body:
                continue
            sender = env.get("envelope", {}).get("source", "")
            sender_name = env.get("envelope", {}).get("sourceName", "")
            ts_ms = int(env.get("envelope", {}).get("timestamp", 0))
            group_info = data_message.get("groupInfo")
            group_id = str(group_info.get("groupId", "")) if group_info else None
            if self._contacts and sender not in self._contacts:
                continue
            timestamp = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
            out.append(
                SignalMessage(
                    envelope_ts_ms=ts_ms,
                    sender_number=sender,
                    sender_name=sender_name,
                    recipient_number=self._account,
                    body=body,
                    timestamp=timestamp,
                    group_id=group_id,
                    raw=dict(env),
                )
            )
        return out

    async def send_message(self, recipient: str, text: str) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._send_sync, recipient, text)

    def _send_sync(self, recipient: str, text: str) -> bool:
        try:
            resp = self._jsonrpc(
                "send",
                {"account": self._account, "recipients": [recipient], "message": text},
            )
            if "error" in resp:
                logger.warning("Signal send error: %s", resp["error"])
                return False
            return True
        except Exception:
            logger.exception("Signal send failed to %s", recipient)
            return False
