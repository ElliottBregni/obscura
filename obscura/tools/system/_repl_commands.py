"""Runtime tools for executing REPL commands from inside the agent loop.

Two tools:

* ``run_slash_command`` — invoke a Python slash command (``/init``, ``/agent``,
  ``/diff`` …) defined in ``obscura.cli.commands.COMMANDS``. Captures any rich
  output emitted by the handler and returns it.
* ``run_at_command`` — resolve a markdown ``@command`` from
  ``~/.obscura/commands/`` and return its body with ``$ARGUMENTS`` substituted.
  The agent should treat the body as a sub-prompt to follow.

The slash-command path needs a REPL-side bridge because ``/`` handlers take a
``REPLContext`` and write to a ``rich.console.Console`` — neither of which the
agent loop has direct access to. The REPL registers a callback at startup via
:meth:`SlashBridge.set_callback`; the tool calls into that. When unregistered
(non-REPL contexts like one-shot CLI runs), the tool returns a clean error.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from obscura.core.context_lazy import LazyCommandLoader
from obscura.core.paths import resolve_all_commands_dirs
from obscura.core.tools import tool

logger = logging.getLogger(__name__)


# Callback signature: (name, arguments) -> awaitable[(captured_output, handler_return)]
SlashRunner = Callable[[str, str], Awaitable[tuple[str, str | None]]]


class SlashBridge:
    """Holds the REPL-installed callback that runs ``/`` slash commands."""

    runner: ClassVar[SlashRunner | None] = None

    @classmethod
    def set_callback(cls, runner: SlashRunner | None) -> None:
        """Install (or clear) the runner. Called by the REPL at startup."""
        cls.runner = runner


# mtime-aware loader; safe to reuse across tool calls.
_loader_singleton: LazyCommandLoader | None = None


def _loader() -> LazyCommandLoader:
    global _loader_singleton
    if _loader_singleton is None:
        _loader_singleton = LazyCommandLoader(resolve_all_commands_dirs())
    return _loader_singleton


@tool(
    "run_slash_command",
    (
        "Execute a built-in Python slash command (e.g. '/init', '/agent', "
        "'/diff', '/status'). Returns the captured console output plus any "
        "handler return value. Use when a prompt or document references a "
        "/command and you want to actually run it. Only available inside the "
        "interactive REPL — returns 'no_repl_bridge' otherwise."
    ),
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Slash command name without the leading '/' (e.g. 'init')."
                ),
            },
            "arguments": {
                "type": "string",
                "description": (
                    "Argument string passed to the handler verbatim. May be empty."
                ),
            },
        },
        "required": ["name"],
    },
)
async def run_slash_command(name: str, arguments: str = "") -> str:
    if not name:
        return json.dumps({"ok": False, "error": "missing_name"})
    if name.startswith("/"):
        name = name[1:]
    runner = SlashBridge.runner
    if runner is None:
        return json.dumps(
            {
                "ok": False,
                "error": "no_repl_bridge",
                "detail": (
                    "Slash commands can only run inside the interactive REPL. "
                    "Run obscura without one-shot mode to use them."
                ),
            },
        )
    try:
        captured, ret = await runner(name, arguments or "")
    except KeyError:
        logger.debug("unknown slash command: /%s", name, exc_info=True)
        return json.dumps(
            {"ok": False, "error": "unknown_slash_command", "name": name},
        )
    except Exception as exc:
        logger.exception("run_slash_command failed for /%s: %s", name, exc)
        return json.dumps(
            {"ok": False, "error": "handler_failed", "name": name, "detail": str(exc)},
        )
    payload: dict[str, Any] = {"ok": True, "name": name, "output": captured}
    if ret is not None:
        payload["return"] = ret
    return json.dumps(payload)


@tool(
    "run_at_command",
    (
        "Resolve and return the body of a markdown @command from "
        "~/.obscura/commands/, with $ARGUMENTS substituted. Treat the "
        "returned body as a sub-prompt to follow. Fuzzy-matches typos and "
        "tells you when it did so via 'inferred_from'. Use this when a "
        "prompt or document references @<name> and you want to expand it."
    ),
    {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "@command name without the leading '@'.",
            },
            "arguments": {
                "type": "string",
                "description": (
                    "Whole post-command argument string. Substituted into "
                    "$ARGUMENTS in the command body. May be empty."
                ),
            },
        },
        "required": ["name"],
    },
)
async def run_at_command(name: str, arguments: str = "") -> str:
    if not name:
        return json.dumps({"ok": False, "error": "missing_name"})
    if name.startswith("@"):
        name = name[1:]
    loader = _loader()
    resolved = loader.resolve_command(name, arguments or "")
    if resolved is None:
        return json.dumps(
            {
                "ok": False,
                "error": "command_not_found",
                "name": name,
                "did_you_mean": loader.suggest_commands(name, limit=5),
            },
        )
    return json.dumps(
        {
            "ok": True,
            "name": resolved.name,
            "description": resolved.description,
            "inferred_from": resolved.inferred_from,
            "argument_hint": resolved.meta.argument_hint,
            "allowed_tools": resolved.meta.allowed_tools,
            "body": resolved.body,
        },
    )


@tool(
    "list_commands",
    (
        "List available commands the agent can run. Returns both '/' "
        "slash commands and '@' markdown commands with their descriptions."
    ),
    {"type": "object", "properties": {}},
)
async def list_commands() -> str:
    at_metas = _loader().discover_commands()
    at_items = sorted(
        ({"name": m.name, "description": m.description} for m in at_metas),
        key=lambda x: x["name"],
    )
    slash_items: list[dict[str, str]] = []
    try:
        # Necessarily lazy: this module sits under obscura.tools.system, and
        # obscura.cli.commands imports obscura.tools.system at module top —
        # importing obscura.cli.commands at the top of this file would create
        # a cycle (commands → tools.system.__init__ → _repl_commands → commands).
        from obscura.cli.commands import COMMANDS as _SLASH_COMMANDS

        for name in sorted(_SLASH_COMMANDS):
            handler = _SLASH_COMMANDS[name]
            doc = (handler.__doc__ or "").strip().split("\n", 1)[0]
            slash_items.append({"name": name, "description": doc})
    except ImportError:
        logger.debug("suppressed exception in list_commands", exc_info=True)
    return json.dumps(
        {
            "ok": True,
            "slash_commands": slash_items,
            "at_commands": at_items,
        },
    )
