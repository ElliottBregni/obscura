"""obscura.cli.renderer.adapters.tool_shell — Shell tool refinement.

The shell tool returns a JSON-serialised result with stdout/stderr/
exit_code. This adapter extracts a friendlier title from the command
and tags exit_code != 0 as ERROR severity even when the tool itself
didn't flag :attr:`AgentEvent.is_error` (a non-zero exit is a tool-
level failure that the renderer should style accordingly).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, override

from obscura.cli.renderer.adapters.base import EventAdapter
from obscura.cli.renderer.adapters.runtime import RuntimeEventAdapter
from obscura.cli.renderer.ui_event import UiEvent, UiEventKind, UiSeverity
from obscura.core.enums.agent import AgentEventKind
from obscura.core.types import AgentEvent

# Tool names that produce shell-shaped JSON results.
_SHELL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "run_command",
        "shell_exec",
        "bash",
    }
)


class ShellToolEventAdapter(EventAdapter):
    """Refines tool_call/tool_result events for shell tools."""

    def __init__(self) -> None:
        self._fallback = RuntimeEventAdapter()

    @override
    def handles(self, event: AgentEvent) -> bool:
        if event.kind not in (
            AgentEventKind.TOOL_CALL,
            AgentEventKind.TOOL_RESULT,
            AgentEventKind.TOOL_CALL_FAILURE,
        ):
            return False
        return (event.tool_name or "") in _SHELL_TOOL_NAMES

    @override
    def adapt(self, event: AgentEvent) -> Iterable[UiEvent]:
        for ui in self._fallback.adapt(event):
            ui.provider = ui.provider or "shell"

            if ui.kind == UiEventKind.TOOL_CALL:
                cmd_raw: Any = (event.tool_input or {}).get("command", "")
                cmd: str
                if isinstance(cmd_raw, list):
                    cmd = " ".join(str(x) for x in cmd_raw)  # type: ignore[reportUnknownArgumentType]
                else:
                    cmd = str(cmd_raw) if cmd_raw else ""
                if cmd:
                    ui.title = f"$ {cmd}"

            elif ui.kind == UiEventKind.TOOL_RESULT:
                exit_code, stdout, stderr = _parse_shell_result(event.tool_result or "")
                if exit_code is not None:
                    ui.metadata = {
                        **ui.metadata,
                        "exit_code": exit_code,
                        "has_stderr": bool(stderr),
                    }
                    if exit_code != 0:
                        ui.severity = UiSeverity.ERROR
                if stderr and exit_code != 0 and not stdout:
                    # Surface stderr as the body when the command failed
                    # silently on stdout — clearer than seeing the JSON.
                    ui.content = stderr

            yield ui


def _parse_shell_result(raw: str) -> tuple[int | None, str, str]:
    """Best-effort parse of shell tool JSON.

    Returns ``(exit_code | None, stdout, stderr)``. Never raises.
    """
    if not raw:
        return None, "", ""
    try:
        parsed: Any = json.loads(raw)
    except (TypeError, ValueError):
        return None, raw, ""
    if not isinstance(parsed, dict):
        return None, raw, ""
    parsed_dict: dict[str, Any] = parsed  # type: ignore[reportUnknownVariableType]
    exit_code_raw: Any = parsed_dict.get("exit_code")
    exit_code: int | None
    if isinstance(exit_code_raw, bool):
        exit_code = int(exit_code_raw)
    elif isinstance(exit_code_raw, int):
        exit_code = exit_code_raw
    else:
        exit_code = None
    stdout: Any = parsed_dict.get("stdout")
    stderr: Any = parsed_dict.get("stderr")
    return (
        exit_code,
        stdout if isinstance(stdout, str) else "",
        stderr if isinstance(stderr, str) else "",
    )
