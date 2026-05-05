"""obscura.core.tool_bridge — Cross-backend tool dispatch helpers.

Lives outside the v1/v2 agent loop split so both can use the same
parameter-aliasing, structural bridging, JSON-Schema coercion, and
truncation logic. Extracted from v1's ``agent_loop.AgentLoop._call_handler``
so it survives the v1 deletion.

Public API:

- :data:`PARAMETER_ALIASES` — provider-specific parameter rename table.
- :data:`TOOL_BRIDGES` — structural input/output transforms keyed by
  canonical tool name.
- :func:`call_tool_handler` — the canonical dispatch entry point. Replaces
  ``obscura.core.agent_loop.call_tool_handler``.
- :data:`MAX_TOOL_RESULT_SIZE` / :func:`maybe_truncate_result` — large
  tool output truncation with on-disk persistence.

Internal callers (v1, v2 dispatch, provider wrappers) all funnel through
:func:`call_tool_handler` so a tool's bridging behavior is identical
regardless of which path invoked it.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

    from obscura.core.types import ToolSpec


logger = logging.getLogger(__name__)


__all__ = [
    "MAX_TOOL_RESULT_SIZE",
    "PARAMETER_ALIASES",
    "TOOL_BRIDGES",
    "TOOL_RESULT_CACHE_DIR",
    "call_tool_handler",
    "maybe_truncate_result",
]


# ---------------------------------------------------------------------------
# Result size cap + on-disk overflow
# ---------------------------------------------------------------------------

MAX_TOOL_RESULT_SIZE = 200 * 1024  # 200 KB (measured in UTF-8 bytes)
TOOL_RESULT_CACHE_DIR = Path("~/.cache/obscura/tool-results").expanduser()


def maybe_truncate_result(result: str, tool_name: str, tool_use_id: str) -> str:  # noqa: ARG001 — tool_name kept for log context
    """If *result* exceeds :data:`MAX_TOOL_RESULT_SIZE` bytes, write the full
    text to disk and return a truncated preview pointing at the cached file.

    Truncation cuts on the last newline boundary within the byte budget so
    we never split a multi-byte character.
    """
    encoded = result.encode("utf-8")
    if len(encoded) <= MAX_TOOL_RESULT_SIZE:
        return result

    TOOL_RESULT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    result_path = TOOL_RESULT_CACHE_DIR / f"{tool_use_id}.txt"
    result_path.write_text(result, encoding="utf-8")

    truncated = encoded[:MAX_TOOL_RESULT_SIZE].decode("utf-8", errors="ignore")
    last_nl = truncated.rfind("\n")
    if last_nl > 0:
        truncated = truncated[:last_nl]

    return (
        f"{truncated}\n\n"
        f"[Result truncated — {len(result):,} chars total. "
        f"Full result saved to: {result_path}]"
    )


# ---------------------------------------------------------------------------
# Parameter aliases — provider-specific name → canonical tool param name
# ---------------------------------------------------------------------------

PARAMETER_ALIASES: dict[str, dict[str, str]] = {
    "write_text_file": {
        "content": "text",  # Copilot/OpenAI uses 'content', we use 'text'
        "file_path": "path",
        "filepath": "path",
    },
    "read_text_file": {
        "file_path": "path",
        "filepath": "path",
    },
    "append_text_file": {
        "content": "text",
        "file_path": "path",
        "filepath": "path",
    },
    "edit_text_file": {
        "file_path": "path",
        "filepath": "path",
        "old_string": "old_text",
        "new_string": "new_text",
        "oldText": "old_text",
        "newText": "new_text",
    },
    "run_shell": {
        "cmd": "script",
        "workdir": "cwd",
        "timeout": "timeout_seconds",
    },
    "todo_write": {
        "plan": "todos",
    },
    "find_files": {
        "head_limit": "max_results",
    },
    "task": {
        "subagent_type": "target",
    },
}


# ---------------------------------------------------------------------------
# Structural bridges — input/output transforms when simple aliasing isn't enough
# ---------------------------------------------------------------------------


def _bridge_grep_input(inputs: dict[str, Any]) -> dict[str, Any]:
    """Map Claude Code Grep flags to ``grep_files`` canonical params."""
    if "-i" in inputs:
        val = inputs.pop("-i")
        if val and "case_sensitive" not in inputs:
            inputs["case_sensitive"] = False
    if "-A" in inputs:
        inputs.setdefault("after_context", inputs.pop("-A"))
    if "-B" in inputs:
        inputs.setdefault("before_context", inputs.pop("-B"))
    if "-C" in inputs:
        inputs.setdefault("context", inputs.pop("-C"))
    inputs.pop("-n", None)  # Obscura always emits line numbers
    return inputs


def _bridge_task_input(inputs: dict[str, Any]) -> dict[str, Any]:
    """Map Claude Code Agent params to the ``task`` tool."""
    inputs.pop("description", None)
    inputs.pop("isolation", None)
    inputs.pop("run_in_background", None)
    inputs.pop("model", None)
    return inputs


def _bridge_run_shell_input(inputs: dict[str, Any]) -> dict[str, Any]:
    """Map Claude Code Bash params to ``run_shell``."""
    inputs.pop("dangerouslyDisableSandbox", None)
    return inputs


def _bridge_todo_write_input(inputs: dict[str, Any]) -> dict[str, Any]:
    """Map Codex ``update_plan`` payloads to ``todo_write`` items."""
    todos_raw = inputs.get("todos")
    if not isinstance(todos_raw, list):
        return inputs
    todos = cast(list[Any], todos_raw)
    normalized: list[dict[str, str]] = []
    for raw_item in todos:
        if not isinstance(raw_item, dict):
            continue
        raw = cast(dict[str, Any], raw_item)
        content = raw.get("content") or raw.get("step") or raw.get("task") or ""
        status = raw.get("status") or "pending"
        active = raw.get("activeForm") or raw.get("active_form") or content
        normalized.append(
            {
                "content": str(content),
                "status": str(status),
                "activeForm": str(active),
            },
        )
    inputs["todos"] = normalized
    inputs.pop("explanation", None)
    return inputs


TOOL_BRIDGES: dict[
    str,
    tuple[
        Callable[[dict[str, Any]], dict[str, Any]] | None,
        Callable[[str], str] | None,
    ],
] = {
    "grep_files": (_bridge_grep_input, None),
    "task": (_bridge_task_input, None),
    "run_shell": (_bridge_run_shell_input, None),
    "todo_write": (_bridge_todo_write_input, None),
}


# ---------------------------------------------------------------------------
# call_tool_handler — the canonical dispatch entry point
# ---------------------------------------------------------------------------


async def call_tool_handler(spec: ToolSpec, inputs: dict[str, Any]) -> Any:
    """Dispatch a tool call through the shared bridging pipeline.

    Used by both v1's ``AgentLoop._call_handler`` (deprecated) and v2's
    dispatch path, plus the Claude/Copilot provider wrappers.

    Steps in order:

    1. Apply any structural :data:`TOOL_BRIDGES` input transform.
    2. Normalize parameter names per :data:`PARAMETER_ALIASES`.
    3. Pre-validate ``required`` schema fields for clean error messages.
    4. Coerce string-shaped values (``"5"``, ``"true"``) to the JSON-Schema
       type when the LLM sent a string for an int/number/boolean.
    5. Invoke the handler, awaiting if coroutine.
    6. On TypeError ``unexpected keyword argument``, retry with that
       kwarg dropped — gives ``**kwargs``-less handlers cross-backend
       parity without forcing every tool to declare every LLM-convention
       parameter.
    """
    handler = spec.handler

    # Step 1: structural bridge
    bridge = TOOL_BRIDGES.get(spec.name)
    if bridge is not None:
        input_transform, _ = bridge
        if input_transform is not None:
            inputs = input_transform(inputs)

    # Step 2: parameter aliases
    if spec.name in PARAMETER_ALIASES:
        aliases = PARAMETER_ALIASES[spec.name]
        for alias, canonical in aliases.items():
            if alias in inputs:
                if canonical not in inputs:
                    inputs[canonical] = inputs.pop(alias)
                else:
                    logger.warning(
                        "Tool %s: both %r (alias) and %r (canonical) provided; "
                        "dropping alias value",
                        spec.name,
                        alias,
                        canonical,
                    )
                    del inputs[alias]

    # Step 3: required-fields pre-check
    required = spec.parameters.get("required", [])
    if required:
        missing = [p for p in required if p not in inputs]
        if missing:
            props = spec.parameters.get("properties", {})
            hints = ", ".join(
                f"`{p}` ({props.get(p, {}).get('type', '?')})" for p in missing
            )
            raise TypeError(
                f"{spec.name}() missing {len(missing)} required "
                f"positional arguments: {hints}"
            )

    # Step 4: type coercion
    props = spec.parameters.get("properties", {})
    for key, value in list(inputs.items()):
        if value is None:
            continue
        prop_schema = props.get(key)
        if not prop_schema:
            continue
        expected = prop_schema.get("type")
        try:
            if expected == "integer" and not isinstance(value, int):
                inputs[key] = int(value)
            elif expected == "number" and not isinstance(value, (int, float)):
                inputs[key] = float(value)
            elif expected == "boolean" and not isinstance(value, bool):
                if isinstance(value, str):
                    inputs[key] = value.lower() not in ("", "0", "false", "no")
                else:
                    inputs[key] = bool(value)
            elif expected == "string" and not isinstance(value, str):
                inputs[key] = str(value)
        except (ValueError, TypeError):
            logger.debug(
                "Tool %s: could not coerce %s=%r to %s",
                spec.name,
                key,
                value,
                expected,
            )

    # Step 5: invoke handler
    try:
        if inspect.iscoroutinefunction(handler):
            return await handler(**inputs)
        result = handler(**inputs)
        if asyncio.iscoroutine(result):
            return await result
        return result
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        # Step 6: drop the offending kwarg and retry once. Handles
        # cross-backend params (e.g. claude's ``prompt`` on web_fetch)
        # that other backends' handlers don't declare.
        msg = str(exc)
        # Format: "...got an unexpected keyword argument 'foo'"
        marker = "unexpected keyword argument "
        idx = msg.rfind(marker)
        if idx == -1:
            raise
        bad_kw = msg[idx + len(marker) :].strip().strip("'\"")
        if bad_kw not in inputs:
            raise
        inputs.pop(bad_kw)
        if inspect.iscoroutinefunction(handler):
            return await handler(**inputs)
        result = handler(**inputs)
        if asyncio.iscoroutine(result):
            return await result
        return result
