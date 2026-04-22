"""Browser tools exposed to obscura agents via the sidepanel bridge.

Each tool's handler generates a request id, sends a ``browser-tool`` frame
through the native-messaging wire, and awaits a response future. The
sidepanel executes the actual DOM work via ``chrome.scripting.executeScript``
and returns the result.

The bridge is a 3-hop round-trip:

    agent → obscura tool handler (this file)
          → _write_frame({"type": "browser-tool", "op": ...})
          → service worker forwards to sidepanel
          → sidepanel runs chrome.scripting.executeScript
          → sidepanel posts {"type": "browser-tool-response", "id": ...}
          → host resolves future → handler returns

All handlers are async and return a JSON-serialisable result.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Awaitable, Callable

from obscura.core.types import ToolSpec


# Populated by _init_bridge. _request_sender is the host's _write_frame.
_request_sender: Callable[[dict[str, Any]], Awaitable[None]] | None = None
_pending: dict[str, asyncio.Future[Any]] = {}


def init(sender: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
    """Wire the tools to the host's frame-writer."""
    global _request_sender
    _request_sender = sender


def resolve(req_id: str, ok: bool, result: Any, error: str = "") -> None:
    fut = _pending.get(req_id)
    if fut is None or fut.done():
        return
    if ok:
        fut.set_result(result)
    else:
        fut.set_exception(RuntimeError(error or "browser tool failed"))


async def _call(op: str, args: dict[str, Any] | None = None) -> Any:
    import logging

    _log = logging.getLogger("obscura.browser-host")
    if _request_sender is None:
        raise RuntimeError("browser-tool bridge not initialised")
    req_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()
    _pending[req_id] = fut
    _log.info("browser_tool invoke op=%s id=%s args=%r", op, req_id, args)
    try:
        await _request_sender(
            {
                "type": "browser-tool",
                "id": req_id,
                "op": op,
                "args": args or {},
            }
        )
        result = await asyncio.wait_for(fut, timeout=30.0)
        _log.info("browser_tool result op=%s id=%s result_len=%d",
                  op, req_id,
                  len(str(result)) if result is not None else 0)
        return result
    except Exception as exc:
        _log.warning("browser_tool error op=%s id=%s err=%r", op, req_id, exc)
        raise
    finally:
        _pending.pop(req_id, None)


# ---------------------------------------------------------------------------
# Tool handlers


async def read_page(
    max_chars: int = 20000,
    include_html: bool = False,
) -> dict[str, Any]:
    """Get the current tab's title, URL, visible text, and link summary."""
    return await _call(
        "read_page",
        {"max_chars": int(max_chars), "include_html": bool(include_html)},
    )


async def query_selector(selector: str, all: bool = False) -> dict[str, Any]:
    """Query the current tab's DOM. Returns innerText + attrs of matches."""
    return await _call("query_selector", {"selector": str(selector), "all": bool(all)})


async def click(selector: str) -> dict[str, Any]:
    """Click the first element matching the CSS selector."""
    return await _call("click", {"selector": str(selector)})


async def fill(selector: str, value: str) -> dict[str, Any]:
    """Fill an input/textarea matching the selector and fire input events."""
    return await _call(
        "fill", {"selector": str(selector), "value": str(value)}
    )


async def eval_js(expression: str) -> dict[str, Any]:
    """Evaluate a JS expression in the current tab's page context.

    The result is JSON-stringified. Complex objects become ``[object ...]``
    unless the expression itself serialises them. Do NOT pass secrets.
    """
    return await _call("eval_js", {"expression": str(expression)})


async def list_tabs() -> dict[str, Any]:
    """List all tabs in the current window (id, title, url, active)."""
    return await _call("list_tabs", {})


async def switch_tab(tab_id: int) -> dict[str, Any]:
    """Activate a tab by its id."""
    return await _call("switch_tab", {"tab_id": int(tab_id)})


async def navigate(url: str) -> dict[str, Any]:
    """Navigate the current tab to a new URL."""
    return await _call("navigate", {"url": str(url)})


async def screenshot() -> dict[str, Any]:
    """Capture a PNG screenshot of the visible viewport. Returns data URL."""
    return await _call("screenshot", {})


# ---------------------------------------------------------------------------
# Wrapper helpers so return values render well in streamed tool_result frames.


def _json(coro: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[str]]:
    async def wrapped(**kwargs: Any) -> str:
        try:
            out = await coro(**kwargs)
            return json.dumps(out, ensure_ascii=False, indent=2)
        except Exception as exc:
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

    return wrapped


# ---------------------------------------------------------------------------
# ToolSpec catalogue


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="browser_read_page",
        description=(
            "Read the current browser tab: title, URL, visible text (capped), "
            "and a link summary. Use this to see what the user is looking at."
        ),
        parameters={
            "type": "object",
            "properties": {
                "max_chars": {
                    "type": "integer",
                    "default": 20000,
                    "description": "Cap the returned innerText at this length.",
                },
                "include_html": {
                    "type": "boolean",
                    "default": False,
                    "description": "Also include a trimmed HTML snapshot.",
                },
            },
        },
        handler=_json(read_page),
    ),
    ToolSpec(
        name="browser_query_selector",
        description=(
            "Query the current page's DOM with a CSS selector. Returns "
            "innerText + attributes of matching elements."
        ),
        parameters={
            "type": "object",
            "required": ["selector"],
            "properties": {
                "selector": {"type": "string"},
                "all": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, return every match; otherwise first.",
                },
            },
        },
        handler=_json(query_selector),
    ),
    ToolSpec(
        name="browser_click",
        description="Click the first element matching the selector.",
        parameters={
            "type": "object",
            "required": ["selector"],
            "properties": {"selector": {"type": "string"}},
        },
        handler=_json(click),
        side_effects="mutating",
    ),
    ToolSpec(
        name="browser_fill",
        description="Type `value` into an input/textarea matching `selector`.",
        parameters={
            "type": "object",
            "required": ["selector", "value"],
            "properties": {
                "selector": {"type": "string"},
                "value": {"type": "string"},
            },
        },
        handler=_json(fill),
        side_effects="mutating",
    ),
    ToolSpec(
        name="browser_eval",
        description=(
            "Evaluate a JS expression in the page context. Result is "
            "JSON-serialised. Avoid passing secrets."
        ),
        parameters={
            "type": "object",
            "required": ["expression"],
            "properties": {"expression": {"type": "string"}},
        },
        handler=_json(eval_js),
        side_effects="mutating",
    ),
    ToolSpec(
        name="browser_list_tabs",
        description="List tabs in the active Chrome window (id, title, url).",
        parameters={"type": "object", "properties": {}},
        handler=_json(list_tabs),
    ),
    ToolSpec(
        name="browser_switch_tab",
        description="Activate a tab by its numeric id.",
        parameters={
            "type": "object",
            "required": ["tab_id"],
            "properties": {"tab_id": {"type": "integer"}},
        },
        handler=_json(switch_tab),
        side_effects="mutating",
    ),
    ToolSpec(
        name="browser_navigate",
        description="Navigate the active tab to a URL.",
        parameters={
            "type": "object",
            "required": ["url"],
            "properties": {"url": {"type": "string"}},
        },
        handler=_json(navigate),
        side_effects="mutating",
    ),
    ToolSpec(
        name="browser_screenshot",
        description="Screenshot the visible viewport. Returns a PNG data URL.",
        parameters={"type": "object", "properties": {}},
        handler=_json(screenshot),
    ),
]
