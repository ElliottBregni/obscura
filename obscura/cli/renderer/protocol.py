"""obscura.cli.renderer.protocol — Abstract renderer interface.

Defines the contract that the renderer must satisfy.  The REPL event
loop calls ``handle()`` for every ``AgentEvent`` and ``finish()`` at
the end of a turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from obscura.core.types import AgentEvent


@runtime_checkable
class RendererProtocol(Protocol):
    """Contract that all renderers must satisfy."""

    def handle(self, event: AgentEvent) -> None:
        """Process a single agent event (text delta, tool call, etc.)."""
        ...

    def finish(self) -> None:
        """Flush remaining buffers and clean up resources."""
        ...

    def get_accumulated_text(self) -> str:
        """Return all accumulated assistant text for this turn."""
        ...

    def get_thinking_blocks(self) -> list[str]:
        """Return completed thinking/reasoning blocks."""
        ...

    def get_last_thinking(self) -> str:
        """Return the most recent thinking block."""
        ...
