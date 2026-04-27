"""browser_stub — in-memory stand-in for ``BrowserBridgeClient``.

A ``BrowserToolStubBridge`` exposes the same async surface as
:class:`obscura.integrations.browser.client.BrowserBridgeClient` —
``list_tools()``, ``call(name, args)``, ``close()`` — but routes every
op into a tiny scripted ``FakePage`` instead of a real Chrome native
host.  This lets the eval harness exercise the ``browser_*`` tools
deterministically with no extension, no Chrome, and no Playwright
dependency.

Scope is intentionally minimal — just enough surface to exercise the
"read page → click/fill → re-read → assert mutation" eval pattern and
the cheap-vs-CDP escalation case.

Typical use::

    bridge = BrowserToolStubBridge.with_default_page()
    tools = await bridge.list_tools()
    page = await bridge.call("browser_read_page", {})

It can also be plugged into a :class:`obscura.core.tools.ToolRegistry`
via the same proxy-spec helper used by the production client::

    from obscura.integrations.browser.client import _build_proxy_spec
    for raw in await bridge.list_tools():
        registry.register(_build_proxy_spec(raw, bridge))  # type: ignore[arg-type]

Structural compatibility is intentional — the proxy spec only calls
``client.call(name, args)`` on whatever object you hand it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any


# ---------------------------------------------------------------------------
# Fake-page state
# ---------------------------------------------------------------------------


@dataclass
class FakeFormField:
    """Mutable representation of a single ``<input>`` / ``<textarea>``."""

    selector: str
    value: str = ""
    submitted_value: str | None = None  # set when the form "submits"


@dataclass
class FakeButton:
    """Mutable representation of a clickable button.

    A click toggles ``state`` and increments ``click_count``. Tests can
    assert on either to verify mutation.
    """

    selector: str
    label: str
    state: str = "off"  # toggled by click
    click_count: int = 0


@dataclass
class FakePage:
    """In-memory canned page for stub-bridge tests."""

    title: str = "Obscura Eval Fixture"
    url: str = "https://example.test/eval"
    headings: list[str] = field(
        default_factory=lambda: ["Welcome", "Headlines", "Form"],
    )
    links: list[dict[str, str]] = field(
        default_factory=lambda: [
            {"text": "Docs", "href": "https://example.test/docs"},
            {"text": "Issues", "href": "https://example.test/issues"},
            {"text": "Source", "href": "https://example.test/src"},
        ],
    )
    body_text: str = (
        "Welcome to the Obscura eval fixture. Use the form below to submit a value."
    )
    buttons: dict[str, FakeButton] = field(default_factory=dict[str, FakeButton])
    fields: dict[str, FakeFormField] = field(default_factory=dict[str, FakeFormField])
    last_pressed_key: str | None = None
    submit_log: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    # Per-op flags so cases can simulate silent-failure + escalation paths.
    fill_silently_fails: bool = False

    def add_button(self, selector: str, label: str) -> None:
        self.buttons[selector] = FakeButton(selector=selector, label=label)

    def add_field(self, selector: str) -> None:
        self.fields[selector] = FakeFormField(selector=selector)

    @classmethod
    def default(cls) -> FakePage:
        page = cls()
        page.add_button("#toggle", "Toggle")
        page.add_field("#email")
        page.add_field("#search")
        return page


# ---------------------------------------------------------------------------
# Stub bridge
# ---------------------------------------------------------------------------


class BrowserToolStubBridge:
    """Drop-in for :class:`BrowserBridgeClient` backed by a :class:`FakePage`.

    Implements the two methods the rest of the codebase actually depends
    on — ``list_tools()`` and ``call(name, args)`` — plus ``close()`` /
    async-context-manager methods so it slots into existing fixtures.
    """

    # Keep the surface explicit — tools registered here mirror the
    # `browser_*` family the eval cases exercise. Adding more is a
    # one-line append plus a handler below.
    _TOOL_SPECS: list[dict[str, Any]] = [
        {
            "name": "browser_read_page",
            "description": "Read the active fake tab.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_chars": {"type": "integer", "default": 20_000},
                    "include_html": {"type": "boolean", "default": False},
                },
            },
            "side_effects": "none",
        },
        {
            "name": "browser_click",
            "description": "Click the first element matching a selector.",
            "parameters": {
                "type": "object",
                "properties": {"selector": {"type": "string"}},
                "required": ["selector"],
            },
            "side_effects": "mutating",
        },
        {
            "name": "browser_fill",
            "description": "Fill an input identified by a selector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["selector", "value"],
            },
            "side_effects": "mutating",
        },
        {
            "name": "browser_press_key",
            "description": "Dispatch a synthesised keyboard event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "selector": {"type": "string"},
                },
                "required": ["key"],
            },
            "side_effects": "mutating",
        },
        {
            "name": "browser_type_text",
            "description": "CDP-backed text typing (simulated).",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "selector": {"type": "string"},
                },
                "required": ["text"],
            },
            "side_effects": "mutating",
        },
    ]

    def __init__(self, page: FakePage | None = None) -> None:
        self.page: FakePage = page or FakePage.default()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._closed = False

    # -- construction helpers ------------------------------------------------

    @classmethod
    def with_default_page(cls) -> BrowserToolStubBridge:
        return cls(FakePage.default())

    # -- public surface ------------------------------------------------------

    async def list_tools(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        # ``refresh`` matches BrowserBridgeClient's signature; harmless here
        # since the fake list never changes.
        del refresh
        # Yield to the event loop so this is genuinely async like the real one.
        await asyncio.sleep(0)
        # Return a deep-ish copy so callers can mutate without affecting state.
        return [dict(spec) for spec in self._TOOL_SPECS]

    async def call(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Dispatch a tool call against the fake page."""
        del timeout  # accepted for API parity, never blocks
        if self._closed:
            msg = "stub bridge is closed"
            raise RuntimeError(msg)
        await asyncio.sleep(0)
        a = dict(args or {})
        self.calls.append((name, a))

        handler = _HANDLERS.get(name)
        if handler is None:
            msg = f"unknown stub tool: {name}"
            raise RuntimeError(msg)
        return handler(self.page, a)

    async def close(self) -> None:
        self._closed = True

    async def __aenter__(self) -> BrowserToolStubBridge:
        return self

    async def __aexit__(
        self,
        _t: type[BaseException] | None,
        _v: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# Tool handlers — each takes ``(page, args)`` and returns a JSON-y dict.
# ---------------------------------------------------------------------------


def _h_read_page(page: FakePage, _args: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": page.title,
        "url": page.url,
        "headings": list(page.headings),
        "links": [dict(link) for link in page.links],
        "text": page.body_text,
        "fields": [
            {"selector": f.selector, "value": f.value} for f in page.fields.values()
        ],
        "buttons": [
            {"selector": b.selector, "label": b.label, "state": b.state}
            for b in page.buttons.values()
        ],
    }


def _h_click(page: FakePage, args: dict[str, Any]) -> dict[str, Any]:
    selector = str(args.get("selector") or "")
    btn = page.buttons.get(selector)
    if btn is None:
        msg = f"no button matches selector: {selector!r}"
        raise RuntimeError(msg)
    btn.click_count += 1
    btn.state = "off" if btn.state == "on" else "on"
    return {"clicked": selector, "state": btn.state, "click_count": btn.click_count}


def _h_fill(page: FakePage, args: dict[str, Any]) -> dict[str, Any]:
    selector = str(args.get("selector") or "")
    value = str(args.get("value") or "")
    field_obj = page.fields.get(selector)
    if field_obj is None:
        msg = f"no field matches selector: {selector!r}"
        raise RuntimeError(msg)
    if page.fill_silently_fails:
        # Return a "success" envelope but DO NOT mutate the field.
        # Mirrors a real-world site that reverts an isTrusted=false write.
        return {"filled": selector, "value": field_obj.value}
    field_obj.value = value
    return {"filled": selector, "value": value}


def _h_press_key(page: FakePage, args: dict[str, Any]) -> dict[str, Any]:
    key = str(args.get("key") or "")
    page.last_pressed_key = key
    selector = args.get("selector")
    # Pressing Enter on a known field "submits" — record the field's
    # current value so eval cases can assert end-to-end form round-trip.
    if key == "Enter" and isinstance(selector, str):
        field_obj = page.fields.get(selector)
        if field_obj is not None:
            field_obj.submitted_value = field_obj.value
            page.submit_log.append(
                {"selector": selector, "value": field_obj.value},
            )
            return {"key": key, "submitted": True, "value": field_obj.value}
    return {"key": key, "submitted": False}


def _h_type_text(page: FakePage, args: dict[str, Any]) -> dict[str, Any]:
    """CDP-style typing — bypasses the silent-failure flag.

    The whole point of escalating from ``browser_fill`` to
    ``browser_type_text`` is that CDP fires real ``isTrusted=true``
    events that sites can't ignore. We model that by always writing.
    """
    text = str(args.get("text") or "")
    selector = args.get("selector")
    if isinstance(selector, str):
        field_obj = page.fields.get(selector)
        if field_obj is None:
            msg = f"no field matches selector: {selector!r}"
            raise RuntimeError(msg)
        field_obj.value = text
        return {"typed": selector, "value": text, "via": "cdp"}
    # No selector → just record we typed something at the focused element.
    return {"typed": None, "text": text, "via": "cdp"}


_HANDLERS: dict[str, Any] = {
    "browser_read_page": _h_read_page,
    "browser_click": _h_click,
    "browser_fill": _h_fill,
    "browser_press_key": _h_press_key,
    "browser_type_text": _h_type_text,
}


__all__ = [
    "BrowserToolStubBridge",
    "FakeButton",
    "FakeFormField",
    "FakePage",
]
