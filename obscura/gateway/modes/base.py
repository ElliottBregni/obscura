"""Base class for gateway modes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from obscura.gateway.config import GatewayConfig


class BaseMode(ABC):
    """Abstract base class for gateway operational modes."""

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config

    @classmethod
    @abstractmethod
    async def is_available(cls, config: GatewayConfig) -> bool:
        """Check if this mode is available."""
        pass

    @abstractmethod
    async def start(self) -> None:
        """Start the mode."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the mode."""
        pass

    @abstractmethod
    async def execute_tool(self, tool_name: str, *args: Any, **kwargs: Any) -> Any:
        """Execute a tool."""
        pass

    @abstractmethod
    async def get_status(self) -> dict[str, Any]:
        """Get mode status."""
        pass
