"""
obscura.core.hooks — Event-driven hook registry for the agent loop.

Hooks fire before and after every ``AgentEvent``.  A *before* hook can
modify or suppress an event; an *after* hook observes it (side-effects
only).

Usage::

    from obscura.core.hooks import HookRegistry
    from obscura.core.types import AgentEvent, AgentEventKind

    hooks = HookRegistry()

    # Fire before every TOOL_CALL event
    @hooks.before(AgentEventKind.TOOL_CALL)
    async def log_tool(event: AgentEvent) -> AgentEvent:
        print(f"calling {event.tool_name}")
        return event

    # Fire after every event (wildcard)
    @hooks.after()
    def audit(event: AgentEvent) -> None:
        audit_log.append(event)
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, cast

from obscura.core.types import AgentEvent, AgentEventKind

if TYPE_CHECKING:
    from obscura.manifest.models import HookDefinition

logger = logging.getLogger(__name__)

# A before-hook receives an event and returns a (possibly modified) event,
# or None to suppress it.
BeforeHook = Callable[[AgentEvent], Awaitable[AgentEvent | None] | AgentEvent | None]

# An after-hook receives an event.  Return value is ignored.
AfterHook = Callable[[AgentEvent], Awaitable[None] | None]


class HookRegistry:
    """Registry of before/after hooks keyed by ``AgentEventKind``.

    ``None`` as a key means "all events" (wildcard).
    """

    def __init__(self) -> None:
        self._before: dict[AgentEventKind | None, list[BeforeHook]] = {}
        self._after: dict[AgentEventKind | None, list[AfterHook]] = {}

    # -- registration --------------------------------------------------------

    def before(
        self,
        kind: AgentEventKind | None = None,
    ) -> Callable[[BeforeHook], BeforeHook]:
        """Decorator: register a before-hook.

        Pass ``kind=None`` (or omit) for a wildcard that fires on every event.
        """

        def decorator(fn: BeforeHook) -> BeforeHook:
            self._before.setdefault(kind, []).append(fn)
            return fn

        return decorator

    def after(
        self,
        kind: AgentEventKind | None = None,
    ) -> Callable[[AfterHook], AfterHook]:
        """Decorator: register an after-hook."""

        def decorator(fn: AfterHook) -> AfterHook:
            self._after.setdefault(kind, []).append(fn)
            return fn

        return decorator

    def add_before(
        self,
        callback: BeforeHook,
        kind: AgentEventKind | None = None,
    ) -> None:
        """Imperative registration for before-hooks."""
        self._before.setdefault(kind, []).append(callback)

    def add_after(
        self,
        callback: AfterHook,
        kind: AgentEventKind | None = None,
    ) -> None:
        """Imperative registration for after-hooks."""
        self._after.setdefault(kind, []).append(callback)

    # -- execution -----------------------------------------------------------

    async def run_before(self, event: AgentEvent) -> AgentEvent | None:
        """Run all matching before-hooks in registration order.

        Returns the (possibly modified) event, or ``None`` if any hook
        suppressed it.  Hooks run in order: wildcards first, then
        kind-specific.
        """
        current: AgentEvent | None = event

        # Wildcards
        for hook in self._before.get(None, ()):
            if current is None:
                return None
            current = await self._call_before(hook, current)

        # Kind-specific
        for hook in self._before.get(event.kind, ()):
            if current is None:
                return None
            current = await self._call_before(hook, current)

        return current

    async def run_after(self, event: AgentEvent) -> None:
        """Run all matching after-hooks.  Exceptions are logged, not raised."""
        # Wildcards
        for hook in self._after.get(None, ()):
            await self._call_after(hook, event)

        # Kind-specific
        for hook in self._after.get(event.kind, ()):
            await self._call_after(hook, event)

    # -- internal helpers ----------------------------------------------------

    @staticmethod
    async def _call_before(hook: BeforeHook, event: AgentEvent) -> AgentEvent | None:
        try:
            result = hook(event)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception:
            logger.exception("Before-hook %s failed", hook)
            return event  # don't suppress on hook failure

    @staticmethod
    async def _call_after(hook: AfterHook, event: AgentEvent) -> None:
        try:
            result = hook(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("After-hook %s failed", hook)

    def clear(self) -> None:
        """Remove all registered hooks."""
        self._before.clear()
        self._after.clear()

    @property
    def count(self) -> int:
        """Total number of registered hooks (before + after)."""
        before = sum(len(v) for v in self._before.values())
        after = sum(len(v) for v in self._after.values())
        return before + after

    # -- declarative config --------------------------------------------------

    def merge(self, other: HookRegistry) -> None:
        """Merge another registry's hooks into this one.

        Useful for combining system-level hooks with per-agent hooks.
        """
        for kind, hooks in other._before.items():
            for hook in hooks:
                self._before.setdefault(kind, []).append(hook)
        for kind, hooks in other._after.items():
            for hook in hooks:
                self._after.setdefault(kind, []).append(hook)

    @classmethod
    def from_hook_definitions(
        cls,
        definitions: list[HookDefinition],
    ) -> HookRegistry:
        """Build a :class:`HookRegistry` from declarative hook definitions.

        Maps hook event names to :class:`AgentEventKind` and creates
        callable wrappers for shell command hooks.
        """
        registry = cls()

        event_map: dict[str, AgentEventKind] = {
            "preToolUse": AgentEventKind.TOOL_CALL,
            "postToolUse": AgentEventKind.TOOL_RESULT,
            "sessionStart": AgentEventKind.TURN_START,
            "sessionEnd": AgentEventKind.AGENT_DONE,
        }
        # USER_INPUT may not exist on all builds; add if present
        if hasattr(AgentEventKind, "USER_INPUT"):
            event_map["userPromptSubmitted"] = AgentEventKind.USER_INPUT
        if hasattr(AgentEventKind, "ERROR"):
            event_map["errorOccurred"] = AgentEventKind.ERROR

        for defn in definitions:
            kind = event_map.get(defn.event)
            if defn.event.startswith("pre"):
                hook_fn = _make_command_before_hook(defn)
                registry.add_before(hook_fn, kind)
            else:
                hook_fn_after = _make_command_after_hook(defn)
                registry.add_after(hook_fn_after, kind)

        return registry


# ---------------------------------------------------------------------------
# Shell-command hook wrappers
# ---------------------------------------------------------------------------


def _make_command_before_hook(defn: HookDefinition) -> BeforeHook:
    """Create an async before-hook that runs a shell command.

    The command receives event data as JSON on stdin.  If it outputs
    ``{"permissionDecision": "deny"}``, the hook returns None to
    suppress the event.
    """

    async def _hook(event: AgentEvent) -> AgentEvent | None:
        if not defn.bash:
            return event
        try:
            payload = json.dumps({
                "event": event.kind.value,
                "tool_name": event.tool_name,
                "tool_input": event.tool_input,
            })
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", defn.bash,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(payload.encode()),
                timeout=defn.timeout_sec,
            )
            if stdout:
                result: Any = json.loads(stdout.decode())
                if isinstance(result, dict):
                    res_dict = cast("dict[str, Any]", result)
                    if res_dict.get("permissionDecision") == "deny":
                        return None
        except asyncio.TimeoutError:
            logger.warning("Hook command timed out: %s", defn.bash)
        except Exception:
            logger.warning("Hook command failed: %s", defn.bash, exc_info=True)
        return event

    return _hook


def _make_command_after_hook(defn: HookDefinition) -> AfterHook:
    """Create an async after-hook that runs a shell command."""

    async def _hook(event: AgentEvent) -> None:
        if not defn.bash:
            return
        try:
            payload = json.dumps({
                "event": event.kind.value,
                "tool_name": event.tool_name,
                "tool_result": event.tool_result if hasattr(event, "tool_result") else "",
            })
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", defn.bash,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(
                proc.communicate(payload.encode()),
                timeout=defn.timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.warning("Hook command timed out: %s", defn.bash)
        except Exception:
            logger.warning("Hook command failed: %s", defn.bash, exc_info=True)

    return _hook
