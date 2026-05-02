#!/usr/bin/env python3
"""Obscura Chrome native-messaging host.

Thin adapter: the wire-protocol on stdin/stdout binds to obscura's real
``ObscuraSession`` / ``REPLContext`` / ``handle_command`` machinery so
every terminal feature is available in the browser panel WITHOUT forking
the core code. The host owns:

  * framing   — 4-byte little-endian length + JSON
  * renderer  — a ``RendererProtocol`` implementation that translates
                ``AgentEvent`` → wire frames (``chunk``, ``tool_start``,
                ``tool_result``, …)
  * widgets   — monkey-patched ``obscura.cli.widgets`` so
                ``confirm_tool`` / ``confirm_attention`` / … round-trip
                through the side panel
  * bridges   — ``browser-tool`` frames that let obscura call into the
                active tab's DOM via ``chrome.scripting.executeScript``

Everything else — $skill and @command parsing, vector memory, session
resume, skill context, auto-compact, permission modes, KAIROS — lives
in ``obscura.cli.session.ObscuraSession`` and is invoked here as-is.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import re
import struct
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any, cast


# ---------------------------------------------------------------------------
# Logging — stdout is the wire, stderr is the log sink.

_LOG_DIR = Path(os.environ.get("OBSCURA_HOME") or (Path.home() / ".obscura")) / "logs"
try:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_PATH = _LOG_DIR / "browser-extension-host.log"
    logging.basicConfig(
        filename=str(_LOG_PATH),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
except Exception:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)

log = logging.getLogger("obscura.browser-host")

VERSION = "0.4.0"  # multi-profile safety: per-host pid files, profile_id tagging

# Browser profile id — learned from the first frame that carries one.
# Panels generate a stable UUID per Chrome profile and send it on every
# send / command / ping message. Host logs it so teammates sharing a
# machine can tell whose session is whose.
_profile_id: str | None = None

log.info(
    "boot: python=%s version=%s cwd=%s pid=%d",
    sys.executable,
    sys.version.split()[0],
    os.getcwd(),
    os.getpid(),
)


# ---------------------------------------------------------------------------
# Native-messaging framing


def _read_frame() -> dict[str, Any] | None:
    header = sys.stdin.buffer.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("<I", header)
    if length == 0:
        return {}
    payload = sys.stdin.buffer.read(length)
    if len(payload) < length:
        return None
    try:
        decoded: Any = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        log.exception("Invalid JSON frame")
        return {}
    if not isinstance(decoded, dict):
        return {}
    return cast("dict[str, Any]", decoded)


_write_lock = asyncio.Lock()


async def _write_frame(obj: dict[str, Any]) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    header = struct.pack("<I", len(data))
    async with _write_lock:
        # Chrome caps messages at 1 MB from host; chunk long payloads.
        MAX = 900_000
        if len(data) <= MAX or obj.get("type") != "chunk":
            sys.stdout.buffer.write(header)
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            return
        text = obj.get("text", "") or ""
        for i in range(0, len(text), MAX // 2):
            piece = {**obj, "text": text[i : i + MAX // 2]}
            p = json.dumps(piece, ensure_ascii=False).encode("utf-8")
            sys.stdout.buffer.write(struct.pack("<I", len(p)))
            sys.stdout.buffer.write(p)
            sys.stdout.buffer.flush()


def _post(obj: dict[str, Any]) -> None:
    """Synchronous post for code paths that aren't async (``print`` proxies,
    renderer ``handle`` calls, etc.). Writes directly to the wire — we
    bypass the async lock because Rich's console.print stays on the main
    thread and serialises its own writes.
    """
    try:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    except Exception:
        log.debug("post encode failed", exc_info=True)
        return
    try:
        sys.stdout.buffer.write(struct.pack("<I", len(data)))
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except Exception:
        log.debug("post write failed", exc_info=True)


# ---------------------------------------------------------------------------
# Auth gate — optional API key authentication.

_authenticated = False


def _check_auth(msg: dict[str, Any]) -> bool:
    """Check the extension <-> native-host shared token.

    If ``OBSCURA_AUTH_TOKEN`` is set, every message must supply a matching
    ``auth_token``. If the env var is not set, the native host accepts
    messages from anything Chrome hands it (the pairing is already gated
    by Chrome's native messaging permissions).
    """
    global _authenticated
    if _authenticated:
        return True
    expected = os.environ.get("OBSCURA_AUTH_TOKEN", "")
    if not expected:
        _authenticated = True  # no token configured = no shared-secret check
        return True
    token = msg.get("auth_token") or ""
    if token == expected:
        _authenticated = True
        return True
    return False


# ---------------------------------------------------------------------------
# Browser-tool bridge (DOM access)

_browser_tools_inited = False


def _ensure_browser_tools() -> list[Any]:
    global _browser_tools_inited
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import browser_tools as _bt  # type: ignore[import-not-found]
    except Exception as exc:
        log.warning("browser_tools import failed: %r", exc)
        return []
    if not _browser_tools_inited:
        _bt.init(_write_frame)  # type: ignore[union-attr]
        _browser_tools_inited = True
    return list(_bt.TOOLS)  # type: ignore[arg-type]


def _resolve_browser_tool(req_id: str, ok: bool, result: Any, error: str = "") -> None:
    try:
        import browser_tools as _bt  # type: ignore[import-not-found]

        _bt.resolve(req_id, ok, result, error)  # type: ignore[union-attr]
    except Exception:
        log.exception("browser tool resolve failed")


# ---------------------------------------------------------------------------
# Widget broker — round-trips confirm_* prompts through the side panel.

_pending_widgets: dict[str, asyncio.Future[dict[str, Any]]] = {}


async def _broker_widget(
    *,
    kind: str,
    question: str,
    actions: list[str],
    default: str | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    widget_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict[str, Any]] = loop.create_future()
    _pending_widgets[widget_id] = fut

    payload: dict[str, Any] = {
        "type": "widget",
        "id": widget_id,
        "kind": kind,
        "question": question,
        "actions": actions,
    }
    if default is not None:
        payload["default"] = default
    if detail is not None:
        payload["detail"] = detail
    await _write_frame(payload)
    try:
        return await fut
    finally:
        _pending_widgets.pop(widget_id, None)


def _resolve_widget(widget_id: str, action: str, text: str = "") -> None:
    fut = _pending_widgets.get(widget_id)
    if fut is not None and not fut.done():
        fut.set_result({"action": action, "text": text})


def _install_widget_broker() -> None:
    try:
        from obscura.cli import prompt as _prompt_mod
        from obscura.cli import widgets as _widgets_mod
    except Exception:
        log.exception("widget broker disabled: import failed")
        return

    async def _browser_confirm_prompt(message: str = "Allow? [y/n/always] ") -> str:
        result = await _broker_widget(
            kind="confirm",
            question=message.strip(),
            actions=["allow", "deny", "always_allow"],
            default="deny",
        )
        return {"allow": "y", "always_allow": "always", "deny": "n"}.get(
            result.get("action", "deny"), "n"
        )

    async def _browser_confirm_tool(request: Any) -> Any:
        tool_input = getattr(request, "tool_input", {}) or {}
        preview = {
            k: v
            for k, v in tool_input.items()
            if isinstance(v, (str, int, float, bool))
        }
        result = await _broker_widget(
            kind="tool_confirm",
            question=f"Run tool `{getattr(request, 'tool_name', '?')}`?",
            actions=["allow", "deny", "always_allow"],
            default="deny",
            detail={"tool_name": getattr(request, "tool_name", ""), "input": preview},
        )
        return _widgets_mod.WidgetResult(
            action=result.get("action", "deny"),
            text=result.get("text", ""),
        )

    async def _browser_confirm_attention(request: Any) -> Any:
        actions = list(getattr(request, "actions", ()) or ["ok"])
        result = await _broker_widget(
            kind="attention",
            question=getattr(request, "message", "(no message)"),
            actions=actions,
            default=actions[0] if actions else "ok",
            detail={
                "agent_name": getattr(request, "agent_name", ""),
                "priority": getattr(request, "priority", "normal"),
                "context": getattr(request, "context", {}),
            },
        )
        return _widgets_mod.WidgetResult(
            action=result.get("action", actions[0] if actions else "ok"),
            text=result.get("text", ""),
        )

    async def _browser_confirm_permission(request: Any) -> Any:
        result = await _broker_widget(
            kind="permission",
            question=getattr(request, "message", "Grant permission?"),
            actions=["allow", "deny", "always_allow"],
            default="deny",
            detail={"scope": getattr(request, "scope", "")},
        )
        return _widgets_mod.WidgetResult(
            action=result.get("action", "deny"),
            text=result.get("text", ""),
        )

    async def _browser_ask_model_question(request: Any) -> Any:
        result = await _broker_widget(
            kind="question",
            question=getattr(request, "question", "?"),
            actions=list(getattr(request, "choices", []) or ["reply"]),
            detail={"context": getattr(request, "context", "")},
        )
        return _widgets_mod.WidgetResult(
            action=result.get("action", ""),
            text=result.get("text", ""),
        )

    _prompt_mod.confirm_prompt_async = _browser_confirm_prompt
    _widgets_mod.confirm_tool = _browser_confirm_tool
    _widgets_mod.confirm_attention = _browser_confirm_attention
    _widgets_mod.confirm_permission = _browser_confirm_permission
    _widgets_mod.ask_model_question = _browser_ask_model_question
    log.info("widget broker installed")


# ---------------------------------------------------------------------------
# BrowserRenderer — translates AgentEvents into wire frames.
#
# Installed via monkey-patch on ``obscura.cli.renderer.create_renderer``.
# ObscuraSession.send() calls ``create_renderer()`` internally; by replacing
# the factory we get every streaming event without editing session.py.

_current_msg_id: str = ""  # contextvar-lite: set before send(), read in handle()


class BrowserRenderer:
    """Implements ``RendererProtocol``. Emits frames to the side panel."""

    def __init__(self, **_kwargs: Any) -> None:
        self._acc: list[str] = []
        self._thinking: list[str] = []
        self._last_thinking: str = ""
        self._tool_inputs: dict[str, str] = {}
        self._msg_id = _current_msg_id  # captured at construction

    def handle(self, event: Any) -> None:
        try:
            from obscura.core.types import AgentEventKind
        except Exception:
            return
        kind = getattr(event, "kind", None)
        if kind is AgentEventKind.TEXT_DELTA:
            text = getattr(event, "text", "") or ""
            if text:
                self._acc.append(text)
                _post({"type": "chunk", "id": self._msg_id, "text": text})
        elif kind is AgentEventKind.THINKING_DELTA:
            text = getattr(event, "text", "") or ""
            if text:
                self._last_thinking = (self._last_thinking or "") + text
                _post({"type": "thinking", "id": self._msg_id, "text": text})
        elif kind is AgentEventKind.TOOL_CALL:
            tid = getattr(event, "tool_use_id", "") or uuid.uuid4().hex
            name = getattr(event, "tool_name", "") or ""
            tool_input = getattr(event, "tool_input", {}) or {}
            _post(
                {
                    "type": "tool_start",
                    "id": self._msg_id,
                    "tool_use_id": tid,
                    "tool_name": name,
                }
            )
            try:
                payload = json.dumps(dict(tool_input), ensure_ascii=False)
            except Exception:
                payload = str(tool_input)
            _post(
                {
                    "type": "tool_delta",
                    "id": self._msg_id,
                    "tool_use_id": tid,
                    "delta": payload,
                }
            )
            _post(
                {
                    "type": "tool_end",
                    "id": self._msg_id,
                    "tool_use_id": tid,
                }
            )
        elif kind is AgentEventKind.TOOL_RESULT:
            tid = getattr(event, "tool_use_id", "") or ""
            result: Any = getattr(event, "tool_result", "") or ""
            if isinstance(result, (dict, list)):
                try:
                    result = json.dumps(
                        cast("dict[str, Any] | list[Any]", result),
                        ensure_ascii=False,
                        indent=2,
                    )
                except Exception:
                    result = str(cast("Any", result))
            is_error = bool(getattr(event, "is_error", False))
            _post(
                {
                    "type": "tool_result",
                    "id": self._msg_id,
                    "tool_use_id": tid,
                    "text": str(result),
                    "is_error": is_error,
                }
            )
        elif kind is AgentEventKind.ERROR:
            text = getattr(event, "text", "") or "(unknown error)"
            _post({"type": "error", "id": self._msg_id, "message": str(text)})
        elif kind is AgentEventKind.PLAN_APPROVAL_REQUEST:
            text = getattr(event, "text", "") or ""
            widget_id = uuid.uuid4().hex
            _post(
                {
                    "type": "widget",
                    "id": widget_id,
                    "kind": "plan_approval",
                    "question": "Plan approval requested",
                    "actions": ["approve", "reject", "modify"],
                    "detail": {"plan": text},
                }
            )
            # Store in pending widgets so the response flows through the
            # existing _resolve_widget path. The session picks up the
            # result via the widget broker when it next checks.
            loop = None
            with contextlib.suppress(RuntimeError):
                loop = asyncio.get_running_loop()
            if loop is not None:
                fut: asyncio.Future[dict[str, Any]] = loop.create_future()
                _pending_widgets[widget_id] = fut
        # TURN_START / TURN_COMPLETE / AGENT_DONE / STOP_CHECK — ignored
        # (AGENT_DONE is driven by the session.send() awaitable returning).

    def finish(self) -> None:
        if self._last_thinking:
            self._thinking.append(self._last_thinking)
            self._last_thinking = ""

    def get_accumulated_text(self) -> str:
        return "".join(self._acc)

    def get_thinking_blocks(self) -> list[str]:
        return list(self._thinking)

    def get_last_thinking(self) -> str:
        return self._last_thinking

    def set_session_context(self, **_kwargs: Any) -> None:
        # No-op: the side panel has its own status bar.
        pass


def _install_renderer_factory() -> None:
    try:
        from obscura.cli import renderer as _renderer_mod
    except Exception:
        log.exception("renderer factory install failed")
        return

    def _factory(streaming_status: Any = None) -> Any:
        return BrowserRenderer(_streaming_status=streaming_status)

    _renderer_mod.create_renderer = _factory
    log.info("renderer factory installed (BrowserRenderer)")


# ---------------------------------------------------------------------------
# Console proxy — streams Rich ``console.print`` output from /command
# handlers back to the panel in real time. Keeps terminal formatting stripped.

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class _StreamingTextIO(io.TextIOBase):
    """File-like. Every write becomes a ``chunk`` frame. ANSI escapes stripped."""

    def __init__(self, msg_id_fn: Any) -> None:
        self._msg_id_fn = msg_id_fn
        self._buf: list[str] = []

    def write(self, s: str) -> int:  # type: ignore[override]
        if not s:
            return 0
        clean = _ANSI_RE.sub("", s)
        self._buf.append(clean)
        mid = self._msg_id_fn()
        if mid:
            _post({"type": "chunk", "id": mid, "text": clean})
        return len(s)

    def writable(self) -> bool:  # type: ignore[override]
        return True

    def flush(self) -> None:  # type: ignore[override]
        pass

    def getvalue(self) -> str:
        return "".join(self._buf)


def _install_console_proxy() -> None:
    """Redirect ``obscura.cli.render.console``'s output file to our stream.

    The global ``console`` is bound by name in many ``cmd_*`` modules at
    import time, so replacing the module attribute has no effect. Instead,
    swap the Rich ``Console`` instance's ``file`` (which it reads on every
    write) and disable its color/terminal emulation so chunks land as
    plain text.
    """
    try:
        from obscura.cli import render as _render_mod
    except Exception:
        log.exception("console proxy install failed")
        return

    stream = _StreamingTextIO(lambda: _current_msg_id)
    console = _render_mod.console
    try:
        console.file = stream  # type: ignore[assignment]
        # Force text output — Rich keeps its own notion of color/term state,
        # and we want clean bytes on the wire. reconfigure() rebuilds internal
        # state (width, highlighter) to match.
        console._force_terminal = False  # type: ignore[attr-defined]
        console.no_color = True
        if hasattr(console, "reconfigure"):
            console.reconfigure(  # type: ignore[attr-defined]
                file=stream,
                force_terminal=False,
                color_system=None,
                width=100,
                soft_wrap=True,
            )
        _render_mod._streaming_text_io = stream  # type: ignore[attr-defined]
        log.info("console proxy installed")
    except Exception:
        log.exception("console proxy attach failed")


# ---------------------------------------------------------------------------
# ObscuraSession wrapper

_session: Any = None
_session_lock = asyncio.Lock()
_session_backend: str = ""
_session_workspace: str | None = None
_session_id_override: str | None = None


async def _ensure_session(
    backend: str = "copilot",
    model: str | None = None,
    *,
    workspace: str | None = None,
    session_id: str | None = None,
) -> Any:
    """Lazy-build the ObscuraSession on first use.

    The session is created once per host process; changing backend / model
    mid-stream isn't supported (matches REPL behaviour — user restarts).
    """
    global _session, _session_backend, _session_workspace, _session_id_override
    if _session is not None:
        return _session

    async with _session_lock:
        if _session is not None:
            return _session

        from obscura.cli.session import ObscuraSession, SessionConfig

        compiled_ws = None
        if workspace:
            try:
                from obscura.core.compiler.compile import compile_workspace

                compiled_ws = compile_workspace(workspace, strict=False)
                log.info(
                    "workspace loaded: name=%s agents=%d",
                    compiled_ws.name,
                    len(compiled_ws.agents),
                )
            except Exception:
                log.warning(
                    "workspace '%s' failed to load; ignoring", workspace, exc_info=True
                )

        # Codex runs its own closed tool loop — it only calls tools it
        # discovered through its native ``mcp_servers`` config, not
        # Obscura's ``register_tool()`` path.  Stand up an in-process
        # streamable-HTTP MCP server exposing the browser tools and
        # inject its URL into the Codex session's extra_mcp_servers so
        # Codex can actually call them.  Claude / Copilot / others keep
        # using register_tool() below, unchanged.
        extra_mcp_servers: list[dict[str, Any]] = []
        if backend == "codex":
            url = await _ensure_browser_mcp_server()
            if url:
                extra_mcp_servers.append(
                    {"name": "obscura_browser", "url": url},
                )

        config = SessionConfig(
            backend=backend,
            model=model,
            session_id=session_id,
            max_turns=20,
            tools_enabled=True,
            confirm=False,
            no_default_prompt=False,
            supervise=False,  # no agent fleet — panel is single-user
            compiled_ws=compiled_ws,
            extra_mcp_servers=extra_mcp_servers or None,
        )

        _session = await ObscuraSession.create(config)
        _session_backend = backend
        _session_workspace = workspace
        _session_id_override = session_id

        # Register browser tools on the live client so the agent can call
        # the DOM.  Skipped on Codex — the browser MCP server above
        # already exposes them on Codex's native tool surface; double-
        # registering would show the tools twice in the prompt.
        if backend != "codex":
            for tool in _ensure_browser_tools():
                try:
                    _session.client.register_tool(tool)
                except Exception:
                    log.warning("failed to register %s", tool.name, exc_info=True)
        else:
            # Prime the browser_tools frame-writer so the MCP server's
            # tool handlers can reach the side panel.
            _ensure_browser_tools()

        log.info(
            "session ready: backend=%s sid=%s tools=%d browser_via_mcp=%s",
            backend,
            _session.ctx.session_id,
            len(list(_session.client._tool_registry.all())),
            backend == "codex",
        )
        return _session


_browser_mcp_url: str | None = None


async def _ensure_browser_mcp_server() -> str | None:
    """Start (or reuse) the in-process browser MCP server for Codex.

    Returns the server URL, or ``None`` if startup failed — callers
    should treat a None result as "browser tools unavailable on Codex
    this session" and log rather than crash, so the chat turn still
    proceeds without browser side effects.
    """
    global _browser_mcp_url
    if _browser_mcp_url is not None:
        return _browser_mcp_url
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import browser_mcp_server as _bms  # type: ignore[import-not-found]
    except Exception:
        log.exception("browser_mcp_server import failed — Codex browser tools disabled")
        return None
    try:
        _browser_mcp_url = await _bms.start_browser_mcp()
    except Exception:
        log.exception(
            "browser_mcp_server failed to start — Codex browser tools disabled"
        )
        return None
    return _browser_mcp_url


# ---------------------------------------------------------------------------
# Prompt-side page context assembly (browser-specific — REPL has no analogue).


def _assemble_prompt(prompt: str, context: dict[str, Any] | None) -> str:
    if not context:
        return prompt
    parts: list[str] = []
    url = (context.get("url") or "").strip()
    title = (context.get("title") or "").strip()
    selection = (context.get("selection") or "").strip()
    text = (context.get("text") or "").strip()
    headings_raw: Any = context.get("headings") or []

    if url:
        parts.append(
            f"Current page: {title} <{url}>" if title else f"Current page: <{url}>"
        )
    if isinstance(headings_raw, list) and headings_raw:
        rendered_items: list[str] = []
        for h_item in headings_raw[:40]:  # type: ignore[union-attr]
            if isinstance(h_item, dict) and h_item.get("text"):  # type: ignore[union-attr]
                rendered_items.append(
                    f"  {h_item.get('level', 'h3')}: {h_item.get('text', '')}"  # type: ignore[union-attr]
                )
        rendered = "\n".join(rendered_items)
        if rendered:
            parts.append(f"Headings:\n{rendered}")
    if selection:
        if len(selection) > 20_000:
            selection = selection[:20_000] + "\n…[truncated]"
        parts.append(f"User selection:\n\n{selection}")
    if text and not selection:
        if len(text) > 15_000:
            text = text[:15_000] + "\n…[truncated]"
        parts.append(f"Page text (live DOM):\n\n{text}")
    if not parts:
        return prompt
    return "\n\n".join(parts) + "\n\n---\n\n" + prompt


# ---------------------------------------------------------------------------
# Request handling

_active_sends: dict[str, asyncio.Task[Any]] = {}


async def _handle_send(msg: dict[str, Any]) -> None:
    global _current_msg_id

    msg_id = str(msg.get("id") or "")
    prompt = str(msg.get("prompt") or "").strip()
    backend = str(msg.get("backend") or "copilot")
    model = msg.get("model") or None
    session_id_in = msg.get("session_id") or None
    workspace = msg.get("workspace") or None
    raw_ctx = msg.get("context")
    context: dict[str, Any] = (
        cast("dict[str, Any]", raw_ctx) if isinstance(raw_ctx, dict) else {}
    )

    if not prompt:
        await _write_frame({"type": "error", "id": msg_id, "message": "Empty prompt"})
        return

    try:
        session = await _ensure_session(
            backend=backend,
            model=model,
            workspace=workspace if isinstance(workspace, str) else None,
            session_id=session_id_in if isinstance(session_id_in, str) else None,
        )
    except Exception as exc:
        log.exception("session create failed")
        await _write_frame(
            {
                "type": "error",
                "id": msg_id,
                "message": f"Session create failed: {exc!r}",
                "trace": traceback.format_exc(),
            }
        )
        return

    full_prompt = _assemble_prompt(prompt, context)

    _current_msg_id = msg_id
    try:
        # ObscuraSession.send() does: inline-@agent, vector memory search,
        # $skill-context injection, auto-compact, confirm gates, permission
        # modes, effort-level thinking budget, file-change tracking, auto-save
        # turn, plan-parse, auto-title. Via the patched BrowserRenderer, every
        # agent event streams back to the panel.
        assistant_text = await session.send(full_prompt)
    except asyncio.CancelledError:
        log.info("send cancelled msg_id=%s", msg_id)
        await _write_frame({"type": "error", "id": msg_id, "message": "cancelled"})
        return
    except Exception as exc:
        log.exception("send failed")
        await _write_frame(
            {
                "type": "error",
                "id": msg_id,
                "message": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(),
            }
        )
        return
    finally:
        _current_msg_id = ""

    await _write_frame(
        {
            "type": "done",
            "id": msg_id,
            "session_id": session.ctx.session_id,
            "text_len": len(assistant_text or ""),
        }
    )


async def _handle_command(msg: dict[str, Any]) -> None:
    """Dispatch a slash command through the REAL ``handle_command`` against
    the session's shared REPLContext. Output streams via the console proxy.
    """
    global _current_msg_id

    msg_id = str(msg.get("id") or "")
    raw = str(msg.get("raw") or "").strip()
    if not raw.startswith("/"):
        raw = "/" + raw

    try:
        session = await _ensure_session(backend=str(msg.get("backend") or "copilot"))
    except Exception as exc:
        await _write_frame(
            {
                "type": "error",
                "id": msg_id,
                "message": f"Session unavailable: {exc!r}",
                "trace": traceback.format_exc(),
            }
        )
        return

    try:
        from obscura.cli.commands import handle_command
    except Exception as exc:
        await _write_frame(
            {
                "type": "error",
                "id": msg_id,
                "message": f"Could not import handle_command: {exc!r}",
            }
        )
        return

    _current_msg_id = msg_id
    try:
        result = await handle_command(raw, session.ctx)
    except Exception as exc:
        log.exception("command failed")
        await _write_frame(
            {
                "type": "error",
                "id": msg_id,
                "message": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(),
            }
        )
        return
    finally:
        _current_msg_id = ""

    await _write_frame({"type": "done", "id": msg_id, "command_result": result})


async def _handle_cancel(msg: dict[str, Any]) -> None:
    target = str(msg.get("target_id") or "")
    task = _active_sends.get(target)
    if task is None:
        return
    task.cancel()
    log.info("cancel requested for %s", target)


async def _handle_kairos(msg: dict[str, Any]) -> None:
    action = str(msg.get("action") or "").lower()
    try:
        session = await _ensure_session()
    except Exception:
        return
    try:
        if action == "on":
            if session.kairos_engine is None:
                from obscura.kairos.engine import KairosEngine

                engine = KairosEngine()
                await engine.start()
                session.kairos_engine = engine
                await _write_frame({"type": "kairos", "state": "on"})
            else:
                await _write_frame({"type": "kairos", "state": "already_on"})
        elif action == "off":
            if session.kairos_engine is not None:
                await session.kairos_engine.stop()
                session.kairos_engine = None
                await _write_frame({"type": "kairos", "state": "off"})
    except Exception as exc:
        await _write_frame(
            {"type": "error", "message": f"kairos {action} failed: {exc!r}"}
        )


async def _handle_diag(msg: dict[str, Any]) -> None:
    """Respond with diagnostic information about the current session."""
    msg_id = str(msg.get("id") or "")
    diag: dict[str, Any] = {
        "type": "diag",
        "id": msg_id,
        "version": VERSION,
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "pid": os.getpid(),
        "session_active": _session is not None,
    }

    if _session is not None:
        try:
            diag["session_id"] = _session.ctx.session_id
            diag["backend"] = _session_backend
            diag["workspace"] = _session_workspace
            diag["tool_count"] = len(list(_session.client._tool_registry.all()))
            diag["turn_count"] = (
                len(_session.ctx.turns) if hasattr(_session.ctx, "turns") else -1
            )
        except Exception:
            pass

    # Vector memory status
    try:
        from obscura.vector_memory import VectorMemoryStore

        diag["vector_memory"] = {"backend": VectorMemoryStore.__name__, "status": "ok"}
    except Exception as exc:
        diag["vector_memory"] = {"status": "error", "error": str(exc)}

    # Key-value memory status
    try:
        from obscura.memory import GlobalMemoryStore

        GlobalMemoryStore.get_instance()
        diag["kv_memory"] = {"status": "ok"}
    except Exception as exc:
        diag["kv_memory"] = {"status": "error", "error": str(exc)}

    # KAIROS status
    if _session is not None:
        diag["kairos"] = {
            "active": _session.kairos_engine is not None,
        }

    await _write_frame(diag)


async def _handle_ping(msg: dict[str, Any]) -> None:
    msg_id = str(msg.get("id") or "")
    await _write_frame({"type": "pong", "id": msg_id})


async def _handle_browser_tool_response(msg: dict[str, Any]) -> None:
    rid = msg.get("id") or ""
    if isinstance(rid, str):
        _resolve_browser_tool(
            rid,
            bool(msg.get("ok", False)),
            msg.get("result"),
            str(msg.get("error") or ""),
        )


async def _handle_widget_response(msg: dict[str, Any]) -> None:
    wid = msg.get("widget_id") or ""
    if isinstance(wid, str):
        _resolve_widget(
            wid,
            str(msg.get("action") or ""),
            str(msg.get("text") or ""),
        )


async def _handle_sessions(msg: dict[str, Any]) -> None:
    """List recent sessions for the session picker UI."""
    msg_id = str(msg.get("id") or "")
    sessions: list[dict[str, Any]] = []
    try:
        from obscura.core.event_store import get_event_store  # type: ignore[import-not-found]

        store = get_event_store()  # type: ignore[reportUnknownVariableType]
        recs: list[Any] = list(await store.list_sessions())  # type: ignore[reportUnknownMemberType]
        for r in recs:
            sessions.append(
                {
                    "session_id": str(getattr(r, "id", "")),
                    "summary": str(getattr(r, "summary", "") or ""),
                    "backend": str(getattr(r, "backend", "") or ""),
                    "message_count": int(getattr(r, "message_count", 0) or 0),
                    "created": str(getattr(r, "created_at", ""))
                    if getattr(r, "created_at", None)
                    else "",
                    "status": str(
                        getattr(
                            getattr(r, "status", ""), "value", getattr(r, "status", "")
                        )
                    ),
                }
            )
        # Most recent first, cap at 20
        sessions = sessions[:20]
    except Exception:
        log.debug("session listing failed", exc_info=True)

    await _write_frame({"type": "sessions", "id": msg_id, "sessions": sessions})


# ---------------------------------------------------------------------------
# Intro-frame builders


def _git_commit() -> str | None:
    try:
        import subprocess

        here = os.path.dirname(os.path.abspath(__file__))
        repo = os.path.abspath(os.path.join(here, "..", "..", ".."))
        r = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        return r.stdout.strip() or None
    except Exception:
        return None


def _available_backends() -> list[str]:
    try:
        from obscura.core.types import Backend

        return [b.value for b in Backend]
    except Exception:
        return []


def _available_commands() -> list[dict[str, Any]]:
    try:
        from obscura.cli.commands import COMMANDS
        from obscura.cli.commands import COMPLETIONS as _COMPLETIONS
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, handler in COMMANDS.items():
        if name in seen:
            continue
        seen.add(name)
        doc = (getattr(handler, "__doc__", "") or "").strip().split("\n")[0]
        entry: dict[str, Any] = {"name": name, "doc": doc[:120]}
        subs = _COMPLETIONS.get(name)
        if isinstance(subs, list) and subs:
            entry["subcommands"] = subs
        out.append(entry)
    out.sort(key=lambda e: e["name"])
    return out


def _available_skills() -> list[str]:
    try:
        from obscura.core._default_skills import DEFAULT_SKILLS
        from obscura.core.context_lazy import LazySkillLoader
        from obscura.core.paths import resolve_all_skills_dirs
    except Exception:
        return []
    seen: set[str] = set()
    names: list[str] = []
    try:
        for d in resolve_all_skills_dirs():
            for s in LazySkillLoader(d).discover_skills():
                if s.name not in seen:
                    seen.add(s.name)
                    names.append(s.name)
    except Exception:
        pass
    for k in DEFAULT_SKILLS:
        if k not in seen:
            seen.add(k)
            names.append(k)
    return sorted(names)


def _available_at_commands() -> list[str]:
    try:
        from obscura.core.context_lazy import LazyCommandLoader
        from obscura.core.paths import resolve_all_commands_dirs

        loader = LazyCommandLoader(resolve_all_commands_dirs())
        return sorted(loader.command_names())
    except Exception:
        return []


def _available_workspaces() -> list[str]:
    """List workspace names from ~/.obscura/specs/workspaces/"""
    try:
        ws_dir = (
            Path(os.environ.get("OBSCURA_HOME") or (Path.home() / ".obscura"))
            / "specs"
            / "workspaces"
        )
        if not ws_dir.is_dir():
            return []
        return sorted(
            p.stem
            for p in ws_dir.iterdir()
            if p.suffix in (".yaml", ".yml", ".json") and p.is_file()
        )
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Multi-profile PID tracking
#
# Each Chrome profile spawns its own native host process. Using a single
# shared `browser-host.pid` file produces bogus "another host is running"
# warnings when two profiles are legitimately both running. Instead we
# write `browser-hosts/<pid>.pid` per-process — collisions are impossible,
# and `obscura-browser status` can enumerate the directory.

_pid_dir = _LOG_DIR.parent / "browser-hosts"
_pid_file = _pid_dir / f"{os.getpid()}.pid"


# Each running host has its own pid file, so false-positive collisions are
# gone. ``peer_hosts()`` surfaces the other live hosts for diagnostics —
# two pids is expected when two Chrome profiles are open.
_multi_instance_detected = False


def _peer_hosts() -> list[int]:
    """Return PIDs of other live obscura host processes (not us)."""
    try:
        if not _pid_dir.is_dir():
            return []
        peers: list[int] = []
        for f in _pid_dir.glob("*.pid"):
            try:
                pid = int(f.stem)
            except ValueError:
                continue
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, 0)
                peers.append(pid)
            except (OSError, ProcessLookupError):
                # stale — clean up proactively so `obscura-browser status`
                # doesn't lie.
                try:
                    f.unlink(missing_ok=True)
                except Exception:
                    pass
        return peers
    except Exception:
        return []


async def _acquire_pid_lock() -> None:
    """Write our PID file and log any peer hosts for observability."""
    global _multi_instance_detected
    try:
        _pid_dir.mkdir(parents=True, exist_ok=True)
        _pid_file.write_text(str(os.getpid()))
        peers = _peer_hosts()
        if peers:
            log.info(
                "peer browser hosts running: %s (one per Chrome profile is expected)",
                peers,
            )
            _multi_instance_detected = True
    except Exception:
        log.debug("pid lock failed", exc_info=True)


def _release_pid_lock() -> None:
    """Remove PID file if it belongs to this process."""
    try:
        if _pid_file.exists() and _pid_file.read_text().strip() == str(os.getpid()):
            _pid_file.unlink()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Message routing table
#
# Each entry: (handler_fn, requires_auth, track_in_active_sends)
# "shutdown" is handled separately because it breaks the loop.

# (handler_fn, requires_auth, track_in_active_sends)
# (handler_fn, requires_auth, track_in_active_sends)
_MSG_HANDLERS: dict[str, tuple[Any, bool, bool]] = {
    "send": (_handle_send, True, True),
    "command": (_handle_command, True, True),
    "cancel": (_handle_cancel, False, False),
    "kairos": (_handle_kairos, False, False),
    "diag": (_handle_diag, False, False),
    "list_sessions": (_handle_sessions, False, False),
    "ping": (_handle_ping, False, False),
    "browser-tool-response": (_handle_browser_tool_response, False, False),
    "widget-response": (_handle_widget_response, False, False),
}


# ---------------------------------------------------------------------------
# Main loop


async def _main() -> None:
    _install_widget_broker()
    _install_renderer_factory()
    _install_console_proxy()

    await _acquire_pid_lock()

    await _write_frame(
        {
            "type": "ready",
            "version": VERSION,
            "python": sys.executable,
            "python_version": sys.version.split()[0],
            "git_commit": _git_commit(),
            "backends": _available_backends(),
            "commands": _available_commands(),
            "skills": _available_skills(),
            "at_commands": _available_at_commands(),
            "workspaces": _available_workspaces(),
            "pid": os.getpid(),
            "peers": _peer_hosts(),
        }
    )

    if _multi_instance_detected:
        peers = _peer_hosts()
        await _write_frame(
            {
                "type": "warning",
                "code": "multi_instance",
                "message": (
                    f"Found {len(peers)} other obscura host(s) running "
                    "(one per Chrome profile is normal). PIDs: "
                    + ", ".join(str(p) for p in peers)
                ),
                "peers": peers,
            }
        )

    loop = asyncio.get_running_loop()

    try:
        while True:
            msg = await loop.run_in_executor(None, _read_frame)
            if msg is None:
                break
            msg_type: str = str(msg.get("type") or "")
            msg_id = str(msg.get("id") or "")

            # Capture profile_id from the first frame that carries one.
            # Every subsequent log line is tagged so teammates sharing a
            # machine can correlate their session against this host.
            pid_in_msg = msg.get("profile_id")
            if isinstance(pid_in_msg, str) and pid_in_msg:
                global _profile_id
                if _profile_id != pid_in_msg:
                    _profile_id = pid_in_msg
                    log.info("profile_id=%s (host pid=%d)", _profile_id, os.getpid())

            # Shutdown is handled inline because it breaks the loop.
            if msg_type == "shutdown":
                log.info("shutdown requested")
                for t in list(_active_sends.values()):
                    t.cancel()
                break

            spec = _MSG_HANDLERS.get(msg_type)
            if spec is None:
                log.debug("unhandled message type: %r", msg_type)
                continue

            handler_fn, requires_auth, track = spec

            if requires_auth and not _check_auth(msg):
                await _write_frame(
                    {
                        "type": "auth_required",
                        "id": msg_id,
                        "message": "Authentication required. Provide auth_token.",
                    }
                )
                continue

            task = asyncio.create_task(handler_fn(msg))
            if track and msg_id:
                _active_sends[msg_id] = task
                task.add_done_callback(lambda _, k=msg_id: _active_sends.pop(k, None))
    finally:
        _release_pid_lock()

    # Drain outstanding work.
    if _active_sends:
        await asyncio.gather(*_active_sends.values(), return_exceptions=True)

    # Close the session cleanly so SQLite stores flush.
    global _session
    if _session is not None:
        try:
            await _session.close()
        except Exception:
            log.debug("session close failed", exc_info=True)
        _session = None

    # Shut the in-process browser MCP server if it was started for a
    # Codex session. Safe to call when it never ran.
    try:
        import browser_mcp_server as _bms  # type: ignore[import-not-found]

        await _bms.stop_browser_mcp()
    except Exception:
        log.debug("browser_mcp_server stop failed", exc_info=True)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("Native host crashed")
        raise
