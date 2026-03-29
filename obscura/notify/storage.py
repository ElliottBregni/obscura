from __future__ import annotations
from typing import Protocol, Any, List, Optional
from dataclasses import dataclass

@dataclass
class Message:
    id: str
    user_id: str
    channel: str
    payload: dict
    status: str
    attempts: int = 0

class Storage(Protocol):
    """Abstract storage interface for notify service."""
    async def setup(self) -> None: ...
    async def save_message(self, message: Message) -> None: ...
    async def get_message(self, message_id: str) -> Optional[Message]: ...
    async def list_pending(self, limit: int = 100) -> List[Message]: ...
    async def update_status(self, message_id: str, status: str, attempts: Optional[int] = None) -> None: ...
    async def close(self) -> None: ...
