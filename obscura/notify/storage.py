from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Message:
    id: str
    user_id: str
    channel: str
    payload: dict
    status: str
    attempts: int = 0
    idempotency_key: str | None = None
    last_error: str | None = None


class Storage(Protocol):
    """Abstract storage interface for notify service."""

    async def setup(self) -> None: ...
    async def save_message(self, message: Message) -> str | None: ...
    async def get_message(self, message_id: str) -> Message | None: ...
    async def list_pending(self, limit: int = 100) -> list[Message]: ...
    async def update_status(
        self,
        message_id: str,
        status: str,
        attempts: int | None = None,
        last_error: str | None = None,
    ) -> None: ...
    async def close(self) -> None: ...
