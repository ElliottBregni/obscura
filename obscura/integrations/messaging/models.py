"""Platform-agnostic messaging primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class PlatformMessage:
    """Normalized inbound message independent of transport provider."""

    platform: str
    account_id: str
    channel_id: str
    sender_id: str
    recipient_id: str
    message_id: str
    text: str
    timestamp: datetime
    metadata: dict[str, Any] = field(default_factory=lambda: {})


@dataclass
class ConversationState:
    """Persisted state for one logical conversation."""

    conversation_key: str
    platform: str
    account_id: str
    channel_id: str
    participants: list[str]
    history: list[dict[str, str]] = field(default_factory=lambda: [])
    last_activity_epoch_s: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationState:
        return cls(
            conversation_key=str(data.get("conversation_key", "")),
            platform=str(data.get("platform", "")),
            account_id=str(data.get("account_id", "default")),
            channel_id=str(data.get("channel_id", "")),
            participants=[str(p) for p in data.get("participants", [])],
            history=[
                {"role": str(x.get("role", "")), "text": str(x.get("text", ""))}
                for x in data.get("history", [])
                if isinstance(x, dict)
            ],
            last_activity_epoch_s=float(data.get("last_activity_epoch_s", 0.0) or 0.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation_key": self.conversation_key,
            "platform": self.platform,
            "account_id": self.account_id,
            "channel_id": self.channel_id,
            "participants": list(self.participants),
            "history": list(self.history),
            "last_activity_epoch_s": self.last_activity_epoch_s,
        }


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)
