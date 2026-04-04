"""obscura.tools.browser — Browser automation tool via Playwright.

Provides page navigation, content extraction, screenshots, and
element interaction for web-based tasks.

Requires: ``playwright`` (install with ``uv pip install playwright``
then ``playwright install chromium``).
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any, cast

from obscura.core.tools import tool

if TYPE_CHECKING:
    from obscura.core.types import ToolSpec

logger = logging.getLogger(__name__)

# Module-level browser state.
_browser: Any = None
_page: Any = None


async def _ensure_browser() -> Any:
    """Lazily launch a headless browser and return the page."""
    global _browser, _page
    if _page is not None:
        return _page
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        msg = (
            "Browser tool requires playwright. Install with:\n"
            "  uv pip install playwright && playwright install chromium"
        )
        raise RuntimeError(
            msg,
        )
    pw = await async_playwright().start()
    _browser = await pw.chromium.launch(headless=True)
    _page = await _browser.new_page()
    return _page


async def _close_browser() -> None:
    """Close the browser if open."""
    global _browser, _page
    if _browser is not None:
        with contextlib.suppress(Exception):
            await _browser.close()
    _browser = None
    _page = None


@tool(
    "web_browser",
    (
        "Control a headless browser for web automation. "
        "Operations: navigate, get_content, screenshot, click, fill, evaluate."
    ),
    {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": [
                    "navigate",
                    "get_content",
                    "screenshot",
                    "click",
                    "fill",
                    "evaluate",
                ],
                "description": "Browser operation to perform.",
            },
            "url": {
                "type": "string",
                "description": "URL to navigate to (for 'navigate').",
            },
            "selector": {
                "type": "string",
                "description": "CSS selector (for click/fill).",
            },
            "value": {
                "type": "string",
                "description": "Value to fill (for 'fill') or JS expression (for 'evaluate').",
            },
            "path": {
                "type": "string",
                "description": "File path to save screenshot (for 'screenshot').",
            },
        },
        "required": ["operation"],
    },
)
async def web_browser(
    operation: str,
    url: str = "",
    selector: str = "",
    value: str = "",
    path: str = "",
) -> str:
    try:
        page = await _ensure_browser()
    except RuntimeError as exc:
        return json.dumps(
            {"ok": False, "error": "browser_unavailable", "detail": str(exc)},
        )

    try:
        if operation == "navigate":
            if not url:
                return json.dumps({"ok": False, "error": "missing_url"})
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            return json.dumps(
                {
                    "ok": True,
                    "operation": "navigate",
                    "url": page.url,
                    "title": await page.title(),
                    "status": response.status if response else None,
                },
            )

        if operation == "get_content":
            # Get page text content (stripped of scripts/styles).
            content = await page.evaluate("""
                () => {
                    const clone = document.cloneNode(true);
                    clone.querySelectorAll('script, style, noscript').forEach(el => el.remove());
                    return clone.body ? clone.body.innerText : document.documentElement.innerText;
                }
            """)
            text = str(content)[:50_000]
            return json.dumps(
                {
                    "ok": True,
                    "operation": "get_content",
                    "url": page.url,
                    "title": await page.title(),
                    "content": text,
                    "length": len(text),
                },
            )

        if operation == "screenshot":
            save_path = path or "/tmp/obscura_screenshot.png"
            await page.screenshot(path=save_path, full_page=True)
            return json.dumps(
                {
                    "ok": True,
                    "operation": "screenshot",
                    "path": save_path,
                    "url": page.url,
                },
            )

        if operation == "click":
            if not selector:
                return json.dumps({"ok": False, "error": "missing_selector"})
            await page.click(selector, timeout=10000)
            return json.dumps(
                {
                    "ok": True,
                    "operation": "click",
                    "selector": selector,
                    "url": page.url,
                },
            )

        if operation == "fill":
            if not selector:
                return json.dumps({"ok": False, "error": "missing_selector"})
            await page.fill(selector, value, timeout=10000)
            return json.dumps(
                {
                    "ok": True,
                    "operation": "fill",
                    "selector": selector,
                    "value": value[:100],
                },
            )

        if operation == "evaluate":
            if not value:
                return json.dumps(
                    {
                        "ok": False,
                        "error": "missing_value",
                        "detail": "Provide JS expression in 'value'",
                    },
                )
            result = await page.evaluate(value)
            return json.dumps(
                {
                    "ok": True,
                    "operation": "evaluate",
                    "result": str(result)[:10_000],
                },
            )

        return json.dumps(
            {"ok": False, "error": "unknown_operation", "detail": operation},
        )

    except Exception as exc:
        return json.dumps({"ok": False, "error": "browser_error", "detail": str(exc)})


def get_browser_tool_specs() -> list[ToolSpec]:
    """Return browser tool specs for registration."""
    return [cast("ToolSpec", web_browser.spec)]
