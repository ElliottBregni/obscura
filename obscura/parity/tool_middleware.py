"""Tool record/replay middleware for scenario harness.

Installs hooks into a :class:`HookRegistry` to intercept tool calls and
results for recording or replaying:

- **Record mode**: After each TOOL_RESULT, saves ``{tool_name, input, output}``
  to a JSON fixture file.
- **Replay mode**: Before each TOOL_CALL, looks up the fixture and returns
  the cached result, suppressing real tool execution.
- **Live mode**: No interception; tools execute normally.

Usage::

    from obscura.parity.tool_middleware import ToolRecordReplayMiddleware

    middleware = ToolRecordReplayMiddleware(mode="record", fixtures_dir="/tmp/fixtures")
    middleware.install(hooks)

    # After the run completes:
    middleware.flush()  # write recorded fixtures to disk
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from obscura.core.hooks import HookRegistry
from obscura.core.types import AgentEvent, AgentEventKind

logger = logging.getLogger(__name__)


@dataclass
class ToolFixture:
    """One recorded tool call and its result."""

    tool_name: str
    tool_input: dict[str, Any]
    tool_result: str
    is_error: bool = False


@dataclass
class ToolRecordReplayMiddleware:
    """Hook-based middleware for recording and replaying tool calls.

    Modes:
    - ``"live"``   — no interception
    - ``"record"`` — record tool results to fixtures
    - ``"replay"`` — replay from recorded fixtures
    """

    mode: str = "live"
    fixtures_dir: str = ""
    _recorded: list[ToolFixture] = field(
        default_factory=lambda: list[ToolFixture]()
    )
    _replay_fixtures: list[ToolFixture] = field(
        default_factory=lambda: list[ToolFixture]()
    )
    _replay_index: int = 0

    def install(self, hooks: HookRegistry) -> None:
        """Register hooks for the configured mode."""
        if self.mode == "record":
            hooks.add_after(self._record_result, AgentEventKind.TOOL_RESULT)
        elif self.mode == "replay":
            self._load_fixtures()
            hooks.add_before(self._replay_tool_call, AgentEventKind.TOOL_CALL)

    def _record_result(self, event: AgentEvent) -> None:
        """After-hook: capture tool result for recording."""
        fixture = ToolFixture(
            tool_name=event.tool_name,
            tool_input=dict(event.tool_input) if event.tool_input else {},
            tool_result=event.tool_result,
            is_error=event.is_error,
        )
        self._recorded.append(fixture)

    def _replay_tool_call(self, event: AgentEvent) -> AgentEvent | None:
        """Before-hook: return cached fixture instead of real execution.

        Returns a modified TOOL_RESULT event that replaces the TOOL_CALL,
        or ``None`` to suppress the event if no fixture is available.
        """
        if self._replay_index >= len(self._replay_fixtures):
            logger.warning(
                "No fixture for tool call %d (%s), passing through",
                self._replay_index,
                event.tool_name,
            )
            return event

        fixture = self._replay_fixtures[self._replay_index]
        self._replay_index += 1
        # Return a TOOL_RESULT event with the cached data
        return AgentEvent(
            kind=AgentEventKind.TOOL_RESULT,
            tool_name=fixture.tool_name,
            tool_input=fixture.tool_input,
            tool_result=fixture.tool_result,
            is_error=fixture.is_error,
            turn=event.turn,
        )

    def flush(self) -> None:
        """Write recorded fixtures to disk as JSON."""
        if self.mode != "record" or not self._recorded:
            return

        fixtures_path = Path(self.fixtures_dir)
        fixtures_path.mkdir(parents=True, exist_ok=True)

        data = [
            {
                "tool_name": f.tool_name,
                "tool_input": f.tool_input,
                "tool_result": f.tool_result,
                "is_error": f.is_error,
            }
            for f in self._recorded
        ]

        output_file = fixtures_path / "tool_fixtures.json"
        output_file.write_text(json.dumps(data, indent=2))
        logger.debug("Wrote %d fixtures to %s", len(data), output_file)

    def _load_fixtures(self) -> None:
        """Load fixtures from disk for replay."""
        if not self.fixtures_dir:
            return

        fixtures_path = Path(self.fixtures_dir) / "tool_fixtures.json"
        if not fixtures_path.exists():
            logger.warning("No fixtures file found at %s", fixtures_path)
            return

        raw: list[dict[str, Any]] = json.loads(fixtures_path.read_text())
        self._replay_fixtures = [
            ToolFixture(
                tool_name=str(entry.get("tool_name", "")),
                tool_input=dict(entry.get("tool_input", {})),
                tool_result=str(entry.get("tool_result", "")),
                is_error=bool(entry.get("is_error", False)),
            )
            for entry in raw
        ]
        self._replay_index = 0
        logger.debug("Loaded %d fixtures from %s", len(self._replay_fixtures), fixtures_path)

    @property
    def recorded(self) -> list[ToolFixture]:
        """Access recorded fixtures (for testing)."""
        return self._recorded

    @property
    def replay_fixtures(self) -> list[ToolFixture]:
        """Access loaded replay fixtures (for testing)."""
        return self._replay_fixtures
