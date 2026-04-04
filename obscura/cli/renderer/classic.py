"""obscura.cli.renderer.classic — Adapter for the existing Rich-based renderer.

Wraps ``StreamRenderer`` from ``obscura.cli.render`` so it satisfies
``RendererProtocol`` without modifying the original class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from obscura.cli.render import StreamRenderer

if TYPE_CHECKING:
    from obscura.core.types import AgentEvent


class ClassicRenderer:
    """Thin wrapper around :class:`StreamRenderer` for protocol conformance."""

    def __init__(self, streaming_status: object | None = None) -> None:
        self._inner = StreamRenderer(streaming_status=streaming_status)

    # -- RendererProtocol methods ------------------------------------------

    def handle(self, event: AgentEvent) -> None:
        self._inner.handle(event)

    def finish(self) -> None:
        self._inner.finish()

    def get_accumulated_text(self) -> str:
        return self._inner.get_accumulated_text()

    def get_thinking_blocks(self) -> list[str]:
        return self._inner.get_thinking_blocks()

    def get_last_thinking(self) -> str:
        return self._inner.get_last_thinking()
