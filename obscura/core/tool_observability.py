"""Per-turn tool-list observability.

Phase-2 tier filtering can drop a lot of tools from each per-turn
payload, but until you can *see* the savings you can't tune them. This
module emits a small stats record every time a backend assembles a
per-turn tool list, so callers can watch how core / discovered /
filtered numbers move during a session.

Default consumer is :mod:`logging` at INFO level. Callers that want
metrics, dashboards, or eval-time accounting register an observer via
:func:`register_observer`.

Stats fields:

* ``backend`` — short backend identifier (``"openai"``, ``"copilot"``,
  ``"claude"``, ``"codex"``, ``"agent_loop"``).
* ``registry_total`` — full registered tool count for this backend.
* ``core_count`` — tools in :data:`obscura.core.tool_tiering.CORE_TOOL_NAMES`.
* ``discovered_count`` — tools surfaced via ``tool_search`` for this task.
* ``sent_count`` — tools actually included in the API request payload.
* ``dropped`` — names dropped by tier filter (deferred + undiscovered).

The emission is best-effort: observer exceptions are caught and logged
at debug level so a misbehaving observer can't break a model turn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnToolStats:
    """Snapshot of per-turn tool-list composition for one backend stream."""

    backend: str
    registry_total: int
    core_count: int
    discovered_count: int
    sent_count: int
    dropped: tuple[str, ...] = field(default_factory=lambda: ())

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)

    def short(self) -> str:
        """Compact one-line summary suitable for logger.info."""
        return (
            f"[{self.backend}] tools sent={self.sent_count} "
            f"(core={self.core_count} discovered={self.discovered_count} "
            f"dropped={self.dropped_count} registry={self.registry_total})"
        )


# Module-level observer list. Append-only; reads iterate snapshot.
_observers: list[Callable[[TurnToolStats], None]] = []


def register_observer(callback: Callable[[TurnToolStats], None]) -> None:
    """Register a callback to receive every TurnToolStats emission."""
    if callback not in _observers:
        _observers.append(callback)


def unregister_observer(callback: Callable[[TurnToolStats], None]) -> None:
    """Detach a previously-registered observer. Idempotent."""
    try:
        _observers.remove(callback)
    except ValueError:
        logger.debug(
            "tool_observability: unregister %r not in observer list",
            callback,
        )
        return


def clear_observers() -> None:
    """Drop every registered observer (test helper)."""
    _observers.clear()


def emit_turn_tool_stats(stats: TurnToolStats) -> None:
    """Log + dispatch a per-turn tool-list snapshot.

    Default behavior: log at INFO with ``stats.short()``. Then each
    registered observer is called; observer exceptions are swallowed
    (logged at debug) so they can't break a model turn.
    """
    logger.info(stats.short())
    for cb in tuple(_observers):
        try:
            cb(stats)
        except Exception:
            logger.debug(
                "tool_observability: observer %r raised",
                cb,
                exc_info=True,
            )
