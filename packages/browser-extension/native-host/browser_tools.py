"""browser_tools — browser-side ``ToolSpec``s that the side-panel fulfils.

Architecture
------------
The native host exposes a small set of agent tools whose handlers *don't*
execute Python — they emit a ``browser-tool`` frame to the side panel and
await a matching ``browser-tool-response``.  The panel runs the actual
``chrome.scripting.executeScript`` / ``chrome.tabs`` / ``chrome.runtime``
calls and streams the result back.

Contract with the host
~~~~~~~~~~~~~~~~~~~~~~
The host loads this module once per session and wires it up:

    import browser_tools
    browser_tools.init(write_frame)        # frame-sender callable
    for spec in browser_tools.TOOLS:
        session.client.register_tool(spec)

When a response arrives (message type ``browser-tool-response``) the host
calls :func:`resolve` with ``(id, ok, result, error)``.

Adding a new tool
~~~~~~~~~~~~~~~~~
1. Write an async handler here that delegates to :func:`_call`.
2. Add a :class:`ToolSpec` for it to :data:`TOOLS`.
3. Add a matching ``case`` to ``runBrowserOp`` in ``sidepanel.js``.

Permissions cap what we can do — see ``manifest.json``.  Today the
extension has ``tabs``, ``scripting``, ``activeTab``, ``storage``,
``sidePanel``, ``nativeMessaging`` and ``<all_urls>`` host permissions.
Tools requiring ``cookies``/``downloads``/``debugger`` are intentionally
omitted so enabling them stays an opt-in manifest change.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any
from collections.abc import Awaitable, Callable

from obscura.core.types import ToolSpec

log = logging.getLogger("obscura.browser_tools")

# ---------------------------------------------------------------------------
# Module state — populated by init(), mutated by resolve() and _call().
# ---------------------------------------------------------------------------

_write_frame: Callable[[dict[str, Any]], Awaitable[None]] | None = None
_pending: dict[str, asyncio.Future[Any]] = {}

_DEFAULT_TIMEOUT = 30.0


def init(write_frame: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
    """Register the frame writer used to deliver ``browser-tool`` requests."""
    global _write_frame
    _write_frame = write_frame


def resolve(req_id: str, ok: bool, result: Any, error: str = "") -> None:
    """Resolve a pending RPC. Called by the host on ``browser-tool-response``."""
    fut = _pending.pop(req_id, None)
    if fut is None or fut.done():
        return
    if ok:
        fut.set_result(result)
    else:
        fut.set_exception(RuntimeError(error or "browser tool failed"))


# ---------------------------------------------------------------------------
# RPC primitive
# ---------------------------------------------------------------------------


async def _call(op: str, args: dict[str, Any], *, timeout: float | None = None) -> Any:
    """Send a ``browser-tool`` frame and await the matching response."""
    if _write_frame is None:
        msg = "browser_tools.init() has not been called"
        raise RuntimeError(msg)

    req_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()
    _pending[req_id] = fut

    try:
        await _write_frame(
            {"type": "browser-tool", "id": req_id, "op": op, "args": args},
        )
        return await asyncio.wait_for(fut, timeout=timeout or _DEFAULT_TIMEOUT)
    except TimeoutError as exc:
        msg = f"browser tool '{op}' timed out"
        raise RuntimeError(msg) from exc
    finally:
        _pending.pop(req_id, None)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def _browser_read_page(
    max_chars: int = 20_000,
    include_html: bool = False,
) -> Any:
    return await _call(
        "read_page",
        {"max_chars": int(max_chars), "include_html": bool(include_html)},
    )


async def _browser_query_selector(selector: str, all: bool = False) -> Any:
    return await _call("query_selector", {"selector": selector, "all": bool(all)})


async def _browser_click(selector: str) -> Any:
    return await _call("click", {"selector": selector})


async def _browser_fill(selector: str, value: str) -> Any:
    return await _call("fill", {"selector": selector, "value": value})


async def _browser_eval_js(expression: str) -> Any:
    return await _call("eval_js", {"expression": expression})


async def _browser_list_tabs() -> Any:
    return await _call("list_tabs", {})


async def _browser_switch_tab(tab_id: int) -> Any:
    return await _call("switch_tab", {"tab_id": int(tab_id)})


async def _browser_navigate(url: str) -> Any:
    return await _call("navigate", {"url": url})


async def _browser_screenshot() -> Any:
    return await _call("screenshot", {})


# -- new, permission-free tools ---------------------------------------------


async def _browser_wait_for_selector(
    selector: str,
    timeout_ms: int = 10_000,
) -> Any:
    # Add a small buffer so the RPC timeout exceeds the in-page timeout.
    timeout = (int(timeout_ms) / 1000.0) + 5.0
    return await _call(
        "wait_for_selector",
        {"selector": selector, "timeout_ms": int(timeout_ms)},
        timeout=timeout,
    )


async def _browser_get_selection() -> Any:
    return await _call("get_selection", {})


async def _browser_scroll_to(selector: str, behavior: str = "smooth") -> Any:
    return await _call("scroll_to", {"selector": selector, "behavior": behavior})


async def _browser_new_tab(url: str, active: bool = True) -> Any:
    return await _call("new_tab", {"url": url, "active": bool(active)})


async def _browser_close_tab(tab_id: int) -> Any:
    return await _call("close_tab", {"tab_id": int(tab_id)})


async def _browser_reload_tab(bypass_cache: bool = False) -> Any:
    return await _call("reload_tab", {"bypass_cache": bool(bypass_cache)})


async def _browser_go_back() -> Any:
    return await _call("go_back", {})


async def _browser_go_forward() -> Any:
    return await _call("go_forward", {})


# ---------------------------------------------------------------------------
# ToolSpec registry
# ---------------------------------------------------------------------------


def _param_obj(**props: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON Schema object node with no required fields by default."""
    required = [k for k, v in props.items() if v.pop("_required", False)]
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="browser_read_page",
        description=(
            "Read the active browser tab: title, URL, plain text, headings, "
            "links, and form fields. Use this first to orient on a page."
        ),
        parameters=_param_obj(
            max_chars={
                "type": "integer",
                "minimum": 500,
                "maximum": 200_000,
                "default": 20_000,
                "description": "Truncate page text after this many characters.",
            },
            include_html={
                "type": "boolean",
                "default": False,
                "description": "Include the raw outerHTML of <body> (trimmed).",
            },
        ),
        handler=_browser_read_page,
        side_effects="none",
    ),
    ToolSpec(
        name="browser_query_selector",
        description="Run a CSS selector on the active tab; return matching elements.",
        parameters=_param_obj(
            selector={
                "type": "string",
                "_required": True,
                "description": "CSS selector to evaluate.",
            },
            all={
                "type": "boolean",
                "default": False,
                "description": "If true, return up to 50 matches instead of only the first.",
            },
        ),
        handler=_browser_query_selector,
        side_effects="none",
    ),
    ToolSpec(
        name="browser_click",
        description="Click the first element matching a CSS selector.",
        parameters=_param_obj(
            selector={
                "type": "string",
                "_required": True,
                "description": "CSS selector of the element to click.",
            },
        ),
        handler=_browser_click,
        side_effects="mutating",
    ),
    ToolSpec(
        name="browser_fill",
        description="Fill an input/textarea identified by a CSS selector.",
        parameters=_param_obj(
            selector={"type": "string", "_required": True},
            value={"type": "string", "_required": True},
        ),
        handler=_browser_fill,
        side_effects="mutating",
    ),
    ToolSpec(
        name="browser_eval_js",
        description=(
            "Evaluate a JavaScript expression in the active tab and return its "
            "value. Escape hatch — prefer targeted tools when possible."
        ),
        parameters=_param_obj(
            expression={
                "type": "string",
                "_required": True,
                "description": "JS expression. Wrapped in an IIFE so `return` works.",
            },
        ),
        handler=_browser_eval_js,
        side_effects="mutating",
    ),
    ToolSpec(
        name="browser_list_tabs",
        description="List tabs in the current browser window.",
        parameters=_param_obj(),
        handler=_browser_list_tabs,
        side_effects="none",
    ),
    ToolSpec(
        name="browser_switch_tab",
        description="Make the given tab id the active tab.",
        parameters=_param_obj(
            tab_id={"type": "integer", "_required": True},
        ),
        handler=_browser_switch_tab,
        side_effects="mutating",
    ),
    ToolSpec(
        name="browser_navigate",
        description="Navigate the active tab to a URL.",
        parameters=_param_obj(
            url={"type": "string", "_required": True, "description": "Target URL."},
        ),
        handler=_browser_navigate,
        side_effects="mutating",
    ),
    ToolSpec(
        name="browser_screenshot",
        description="Capture the visible area of the active tab as a PNG data URL.",
        parameters=_param_obj(),
        handler=_browser_screenshot,
        side_effects="none",
    ),
    # -- new tools ----------------------------------------------------------
    ToolSpec(
        name="browser_wait_for_selector",
        description=(
            "Poll the active tab until a CSS selector matches or the timeout "
            "elapses. Use on SPAs before clicking/filling newly rendered nodes."
        ),
        parameters=_param_obj(
            selector={"type": "string", "_required": True},
            timeout_ms={
                "type": "integer",
                "minimum": 100,
                "maximum": 60_000,
                "default": 10_000,
            },
        ),
        handler=_browser_wait_for_selector,
        side_effects="none",
    ),
    ToolSpec(
        name="browser_get_selection",
        description=(
            "Return the user's currently-highlighted text (and its selector "
            "context) in the active tab."
        ),
        parameters=_param_obj(),
        handler=_browser_get_selection,
        side_effects="none",
    ),
    ToolSpec(
        name="browser_scroll_to",
        description="Scroll the active tab so the first match of a selector is visible.",
        parameters=_param_obj(
            selector={"type": "string", "_required": True},
            behavior={
                "type": "string",
                "enum": ["smooth", "auto"],
                "default": "smooth",
            },
        ),
        handler=_browser_scroll_to,
        side_effects="mutating",
    ),
    ToolSpec(
        name="browser_new_tab",
        description="Open a URL in a new tab.",
        parameters=_param_obj(
            url={"type": "string", "_required": True},
            active={
                "type": "boolean",
                "default": True,
                "description": "Focus the new tab after opening.",
            },
        ),
        handler=_browser_new_tab,
        side_effects="mutating",
    ),
    ToolSpec(
        name="browser_close_tab",
        description="Close the tab with the given id.",
        parameters=_param_obj(
            tab_id={"type": "integer", "_required": True},
        ),
        handler=_browser_close_tab,
        side_effects="mutating",
    ),
    ToolSpec(
        name="browser_reload_tab",
        description="Reload the active tab.",
        parameters=_param_obj(
            bypass_cache={
                "type": "boolean",
                "default": False,
                "description": "Force a cache-bypassing reload (Shift-F5 equivalent).",
            },
        ),
        handler=_browser_reload_tab,
        side_effects="mutating",
    ),
    ToolSpec(
        name="browser_go_back",
        description="Navigate the active tab one step back in its history.",
        parameters=_param_obj(),
        handler=_browser_go_back,
        side_effects="mutating",
    ),
    ToolSpec(
        name="browser_go_forward",
        description="Navigate the active tab one step forward in its history.",
        parameters=_param_obj(),
        handler=_browser_go_forward,
        side_effects="mutating",
    ),
]


__all__ = ["TOOLS", "init", "resolve"]
