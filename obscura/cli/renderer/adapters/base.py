r"""obscura.cli.renderer.adapters.base — Adapter contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from obscura.cli.renderer.ui_event import UiEvent
from obscura.core.types import AgentEvent


class EventAdapter(ABC):
    r"""Convert one :class:`AgentEvent` into zero or more :class:`UiEvent`\ s.

    Adapters are pure functions on input — they never mutate the
    incoming :class:`AgentEvent` and never raise. If they don't know
    how to handle an event, ``handles()`` returns ``False`` and the
    normalizer falls through to the next adapter.
    """

    @abstractmethod
    def handles(self, event: AgentEvent) -> bool:
        """Return True if this adapter wants to own ``event``."""

    @abstractmethod
    def adapt(self, event: AgentEvent) -> Iterable[UiEvent]:
        r"""Yield :class:`UiEvent`\ s produced from ``event``.

        Must never raise; on malformed input the adapter should yield
        an error :class:`UiEvent` instead.
        """
