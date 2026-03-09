"""obscura.core.lifecycle_events -- Structured lifecycle event records.

Provides a frozen dataclass for recording lifecycle events across the
Obscura runtime: workspace boot, agent start/stop, plugin load,
tool execution, and preflight results.

Usage::

    from obscura.core.lifecycle_events import LifecycleEvent

    event = LifecycleEvent(
        timestamp=time.time(),
        event_type="agent_start",
        workspace="code-mode",
        agent="reviewer",
        status="ok",
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LifecycleEvent:
    """A structured record of a lifecycle event.

    Fields
    ------
    timestamp : float
        Unix timestamp (seconds since epoch).
    event_type : str
        The kind of event (e.g. ``agent_start``, ``tool_call``, ``preflight_fail``).
    workspace : str
        Name of the active workspace.
    agent : str
        Name of the agent involved.
    plugin : str
        Plugin identifier, if relevant.
    tool : str
        Tool name, if relevant.
    status : str
        Outcome: ``ok``, ``error``, ``denied``, ``skipped``.
    duration_ms : int
        Duration of the operation in milliseconds.
    metadata : dict
        Arbitrary key-value pairs for additional context.
    """

    timestamp: float
    event_type: str
    workspace: str = ""
    agent: str = ""
    plugin: str = ""
    tool: str = ""
    status: str = ""
    duration_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


__all__ = ["LifecycleEvent"]
