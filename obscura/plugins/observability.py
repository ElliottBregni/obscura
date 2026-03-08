"""Plugin observability — structured event tracking for the plugin platform.

Emits ``PluginEvent`` records for plugin lifecycle changes, capability grants,
tool executions, and health state transitions.  Events feed into the existing
``AuditEvent`` pipeline and JSONL trace log.

Usage::

    from obscura.plugins.observability import PluginEventEmitter

    emitter = PluginEventEmitter()
    emitter.plugin_loaded("github", version="1.0.0")
    emitter.tool_executed("github_search", agent_id="agent-1", latency_ms=42)
    for event in emitter.events:
        print(event)
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PluginEvent:
    """A structured event from the plugin subsystem."""

    event_type: str          # e.g. "plugin.loaded", "capability.granted", "tool.executed"
    plugin_id: str = ""
    timestamp: float = field(default_factory=time.time)
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "plugin_id": self.plugin_id,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            **self.details,
        }


class PluginEventEmitter:
    """Collects and forwards plugin events.

    Events are stored in-memory and optionally forwarded to a callback
    (e.g. the AuditEvent pipeline or JSONL logger).
    """

    def __init__(self, sink: Any | None = None, max_events: int = 10_000) -> None:
        self._events: list[PluginEvent] = []
        self._sink = sink
        self._max = max_events

    def _emit(self, event: PluginEvent) -> None:
        self._events.append(event)
        if len(self._events) > self._max:
            self._events = self._events[-self._max:]
        logger.debug("PluginEvent: %s %s", event.event_type, event.plugin_id)
        if self._sink and hasattr(self._sink, "emit"):
            try:
                self._sink.emit(event.to_dict())
            except Exception:
                pass

    # -- Lifecycle events --------------------------------------------------

    def plugin_loaded(self, plugin_id: str, **details: Any) -> None:
        self._emit(PluginEvent("plugin.loaded", plugin_id, details=details))

    def plugin_unloaded(self, plugin_id: str, **details: Any) -> None:
        self._emit(PluginEvent("plugin.unloaded", plugin_id, details=details))

    def plugin_failed(self, plugin_id: str, error: str, **details: Any) -> None:
        self._emit(PluginEvent("plugin.failed", plugin_id, details={"error": error, **details}))

    def plugin_health_changed(self, plugin_id: str, healthy: bool, **details: Any) -> None:
        self._emit(PluginEvent("plugin.health_changed", plugin_id, details={"healthy": healthy, **details}))

    # -- Capability events -------------------------------------------------

    def capability_granted(self, capability_id: str, grantee_id: str, **details: Any) -> None:
        self._emit(PluginEvent("capability.granted", details={
            "capability_id": capability_id, "grantee_id": grantee_id, **details,
        }))

    def capability_denied(self, capability_id: str, grantee_id: str, **details: Any) -> None:
        self._emit(PluginEvent("capability.denied", details={
            "capability_id": capability_id, "grantee_id": grantee_id, **details,
        }))

    # -- Tool events -------------------------------------------------------

    def tool_executed(self, tool_name: str, agent_id: str, **details: Any) -> None:
        self._emit(PluginEvent("tool.executed", details={
            "tool_name": tool_name, "agent_id": agent_id, **details,
        }))

    def tool_denied(self, tool_name: str, agent_id: str, reason: str, **details: Any) -> None:
        self._emit(PluginEvent("tool.denied", details={
            "tool_name": tool_name, "agent_id": agent_id, "reason": reason, **details,
        }))

    def tool_approved(self, tool_name: str, agent_id: str, **details: Any) -> None:
        self._emit(PluginEvent("tool.approved", details={
            "tool_name": tool_name, "agent_id": agent_id, **details,
        }))

    # -- Access ------------------------------------------------------------

    @property
    def events(self) -> list[PluginEvent]:
        return list(self._events)

    def events_since(self, timestamp: float) -> list[PluginEvent]:
        return [e for e in self._events if e.timestamp >= timestamp]

    def events_for_plugin(self, plugin_id: str) -> list[PluginEvent]:
        return [e for e in self._events if e.plugin_id == plugin_id]

    def clear(self) -> None:
        self._events.clear()


__all__ = [
    "PluginEvent",
    "PluginEventEmitter",
]
