"""obscura.providers.codex — BackendProtocol implementation for the official Codex SDK.

This backend uses OpenAI's ``codex_app_server`` SDK (PyPI: ``openai-codex-app-server-sdk``),
which drives the local ``codex`` binary via its ``app-server`` subcommand over
JSON-RPC. Reference: https://developers.openai.com/codex/sdk
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import shutil
import sys
import uuid
from typing import TYPE_CHECKING, Any

from obscura.core.enums.agent import Backend, ChunkKind, HookPoint, Role
from obscura.core.stream_guards import bind_stream_log
from obscura.core.types import (
    BackendCapabilities,
    HookContext,
    Message,
    NativeHandle,
    SessionRef,
    StreamChunk,
    StreamMetadata,
)
from obscura.core.sessions import SessionStore
from obscura.integrations.mcp.discovery import register_external_mcp_tools
from obscura.plugins.capabilities import build_capability_map_section
from obscura.providers._codex_mcp_config import (
    mcp_servers_to_config_overrides as _mcp_servers_to_config_overrides,
)
from obscura.providers._codex_sdk_compat import relax_strict_response_models
from obscura.providers._codex_stream_adapter import (
    items_to_content_blocks,
    map_notification_to_chunks,
    sanitize_tool_name,
    summarize_file_changes,
    unwrap_item,
)
from obscura.providers._tool_host import BackendToolHostMixin
from obscura.providers.registry import ModelInfo as RegistryModelInfo

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from obscura.core.auth import AuthConfig


_SDK_MODULE = "codex_app_server"


_relax_strict_response_models = relax_strict_response_models


class CodexBackend(BackendToolHostMixin):
    """BackendProtocol implementation backed by ``codex_app_server.AsyncCodex``."""

    def __init__(
        self,
        auth: AuthConfig,
        *,
        model: str | None = None,
        system_prompt: str = "",
        mcp_servers: list[dict[str, Any]] | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._auth = auth
        self._model = model or "gpt-5.5"
        self._system_prompt = system_prompt
        self._mcp_servers = mcp_servers or []
        self._reasoning_effort = reasoning_effort or "medium"

        self._init_tool_host()
        self._hooks: dict[HookPoint, list[Callable[..., Any]]] = {
            hp: [] for hp in HookPoint
        }
        self._session_store = SessionStore()
        self._active_session: str | None = None
        self._thread_id_by_session: dict[str, str] = {}
        self._started = False

        self._sdk_client: Any = None
        # SDK symbols cached at start() so we don't re-import on every turn.
        self._sdk_syms: dict[str, Any] = {}

    # -- Provider Registry overrides -----------------------------------------

    async def list_models(self) -> list[RegistryModelInfo]:
        """List models available for Codex."""
        entries: list[tuple[str, str]] = [
            (
                "gpt-5.5",
                "Frontier model for complex coding, research, and real-world work.",
            ),
            ("gpt-5.4", "Strong model for everyday coding."),
            (
                "gpt-5.4-mini",
                "Small, fast, and cost-efficient model for simpler coding tasks.",
            ),
            ("gpt-5.3-codex", "Coding-optimized model."),
            (
                "gpt-5.2",
                "Optimized for professional work and long-running agents.",
            ),
        ]
        return [
            RegistryModelInfo(
                id=mid,
                name=f"{mid} — {desc}",
                provider="codex",
                supports_tools=True,
                supports_vision=False,
            )
            for mid, desc in entries
        ]

    def get_default_model(self) -> str:
        return "gpt-5.5"

    def validate_model(self, model_id: str) -> bool:
        return True  # Codex validates internally

    @property
    def native(self) -> NativeHandle:
        return NativeHandle(
            client=self._sdk_client,
            session=self._active_session,
            meta={
                "provider": _SDK_MODULE,
                "model": self._model,
            },
        )

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_calls=False,  # Codex manages its own tools autonomously
            supports_tool_choice=False,
            supports_usage=True,
            supports_remote_sessions=True,
            supports_native_mode=True,
            native_features=(_SDK_MODULE, "sdk_threads", "autonomous_agent"),
        )

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        await register_external_mcp_tools(self, self._mcp_servers)

        sdk_cls = self._import_sdk_class()
        self._sdk_client = await self._build_sdk_client(sdk_cls)
        self._started = True

    async def stop(self) -> None:
        client = self._sdk_client
        self._sdk_client = None
        self._started = False
        if client is None:
            return
        aexit = getattr(client, "__aexit__", None)
        if callable(aexit):
            try:
                await self._maybe_await(aexit(None, None, None))
                return
            except Exception:
                logger.debug("suppressed exception in stop", exc_info=True)
        close = getattr(client, "close", None)
        if callable(close):
            try:
                await self._maybe_await(close())
            except Exception:
                logger.debug("suppressed exception in stop", exc_info=True)

    # -- Send / Stream -------------------------------------------------------

    async def send(self, prompt: str, **kwargs: Any) -> Message:
        """Send a prompt and wait for the full response."""
        self._ensure_started()
        await self._run_hooks(
            HookContext(hook=HookPoint.USER_PROMPT_SUBMITTED, prompt=prompt),
        )

        thread = await self._resolve_thread()
        run_kwargs = self._build_run_kwargs()
        with bind_stream_log():
            result = await self._maybe_await(thread.run(prompt, **run_kwargs))

        text = getattr(result, "final_response", None) or ""
        items = list(getattr(result, "items", None) or [])

        if not text:
            # Fallback: pick the last agentMessage's text from items.
            for raw in reversed(items):
                inner = self._unwrap_item(raw)
                if getattr(inner, "type", "") == "agentMessage":
                    text = getattr(inner, "text", "") or ""
                    if text:
                        break
        if not text:
            msg = "Codex SDK returned an empty response"
            raise RuntimeError(msg)

        thread_id = getattr(thread, "id", "") or ""
        if self._active_session and thread_id:
            self._thread_id_by_session[self._active_session] = thread_id

        content_blocks = self._items_to_content_blocks(items, text)

        await self._run_hooks(HookContext(hook=HookPoint.STOP))
        return Message(
            role=Role.ASSISTANT,
            content=content_blocks,
            backend=Backend.CODEX,
            model=self._model,
            session_id=self._active_session,
            raw=result,
        )

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        """Send a prompt and yield real-time streaming chunks."""
        self._ensure_started()
        await self._run_hooks(
            HookContext(hook=HookPoint.USER_PROMPT_SUBMITTED, prompt=prompt),
        )

        thread = await self._resolve_thread()
        run_kwargs = self._build_run_kwargs()
        wire_input = self._wrap_text_input(prompt)

        # Bind per-task log so MCP-routed tool calls (which is how Codex
        # invokes obscura tools) hit the dedup/budget guard at
        # ObscuraMCPServer.handle_tools_call.
        with bind_stream_log():
            turn_handle = await self._maybe_await(thread.turn(wire_input, **run_kwargs))

            yield StreamChunk(kind=ChunkKind.MESSAGE_START)

            usage_data: dict[str, int] | None = None
            finish_reason = "stop"

            try:
                event_stream = turn_handle.stream()
                async for event in event_stream:
                    method = getattr(event, "method", "") or ""
                    payload = getattr(event, "payload", None)

                    if method == "thread/started":
                        tid = getattr(payload, "thread_id", "") or ""
                        if self._active_session and tid:
                            self._thread_id_by_session[self._active_session] = tid

                    for chunk in self._map_notification_to_chunks(method, payload):
                        yield chunk

                    if method == "thread/tokenUsage/updated":
                        tu = getattr(payload, "token_usage", None)
                        total = getattr(tu, "total", None)
                        if total is not None:
                            usage_data = {
                                "input_tokens": getattr(total, "input_tokens", 0) or 0,
                                "output_tokens": getattr(total, "output_tokens", 0)
                                or 0,
                                "cached_input_tokens": getattr(
                                    total,
                                    "cached_input_tokens",
                                    0,
                                )
                                or 0,
                            }

                    if method == "turn/completed":
                        turn = getattr(payload, "turn", None)
                        status = getattr(turn, "status", None)
                        status_val = getattr(status, "value", status) if status else ""
                        if status_val == "failed":
                            finish_reason = "error"

                    if method == "error":
                        finish_reason = "error"

            except Exception as exc:
                logger.debug("suppressed exception in stream", exc_info=True)
                yield StreamChunk(kind=ChunkKind.ERROR, text=str(exc))
                finish_reason = "error"

        await self._run_hooks(HookContext(hook=HookPoint.STOP))

        yield StreamChunk(
            kind=ChunkKind.DONE,
            metadata=StreamMetadata(
                finish_reason=finish_reason,
                model_id=self._model,
                usage=usage_data,
                session_id=self._active_session or "",
            ),
        )

    # -- Sessions ------------------------------------------------------------

    async def create_session(self, **kwargs: Any) -> SessionRef:
        session_id = str(uuid.uuid4())
        self._active_session = session_id
        ref = SessionRef(session_id=session_id, backend=Backend.CODEX)
        self._session_store.add(ref)
        return ref

    async def resume_session(self, ref: SessionRef) -> None:
        refs = self._session_store.list_all(Backend.CODEX)
        if not any(r.session_id == ref.session_id for r in refs):
            msg = f"Session {ref.session_id} not found"
            raise RuntimeError(msg)
        self._active_session = ref.session_id

    async def list_sessions(self) -> list[SessionRef]:
        return self._session_store.list_all(Backend.CODEX)

    async def delete_session(self, ref: SessionRef) -> None:
        self._session_store.remove(ref.session_id)
        self._thread_id_by_session.pop(ref.session_id, None)
        if self._active_session == ref.session_id:
            self._active_session = None

    # -- Tools ---------------------------------------------------------------

    # register_tool comes from BackendToolHostMixin

    def register_hook(self, hook: HookPoint, callback: Callable[..., Any]) -> None:
        self._hooks[hook].append(callback)

    # get_tool_registry comes from BackendToolHostMixin

    # -- Internals -----------------------------------------------------------

    def _ensure_started(self) -> None:
        if not self._started:
            msg = "CodexBackend not started. Call start() first."
            raise RuntimeError(msg)

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _sanitize_tool_name(name: str) -> str:
        return sanitize_tool_name(name)

    @staticmethod
    def _unwrap_item(item: Any) -> Any:
        return unwrap_item(item)

    @staticmethod
    def _summarize_file_changes(item: Any) -> str:
        return summarize_file_changes(item)

    def _build_tool_listing(self) -> str:
        """Build a human-readable tool listing for the system prompt.

        Core tools (those in CORE_TOOL_NAMES or passing is_core()) appear with
        full descriptions. Non-core and shadow tools are deferred — they get a
        single compact line directing the model to use ``tool_search`` first.
        """
        from obscura.core.tool_tiering import deferred_listing, split_by_tier

        # Shadow specs (MCP shadows) are never listed in the prompt — they are
        # discoverable via tool_search but bloat the system prompt otherwise.
        visible_tools = [s for s in self._tools if not getattr(s, "is_shadow", False)]
        core_tools, deferred_tools = split_by_tier(visible_tools)

        lines = ["## Available Tools", ""]
        lines.append(
            "You have the following tools. Use these EXACT names when calling tools:",
        )
        lines.append("")
        for spec in core_tools:
            desc = (spec.description or "").split("\n")[0][:120]
            cap_tag = f" [{spec.capability}]" if getattr(spec, "capability", "") else ""
            lines.append(
                f"- `{self._sanitize_tool_name(spec.name)}`{cap_tag}: {desc}",
            )
        lines.append("")
        lines.append(
            "Do NOT invent tool names. If none of these tools fit, tell the user.",
        )
        try:
            cap_section = build_capability_map_section(core_tools)
            if cap_section:
                lines.append("")
                lines.append(cap_section)
        except Exception:
            logger.debug("suppressed exception in _build_tool_listing", exc_info=True)

        if deferred_tools:
            deferred_section = deferred_listing(deferred_tools)
            if deferred_section:
                lines.append("")
                lines.append(deferred_section)

        return "\n".join(lines)

    def _build_system_prompt(self) -> str:
        """Build full system prompt with tool listing appended."""
        prompt = self._system_prompt or ""
        if self._tools:
            tool_section = self._build_tool_listing()
            prompt = f"{prompt}\n\n{tool_section}" if prompt else tool_section
        return prompt

    def _wrap_text_input(self, prompt: str) -> Any:
        """Wrap a prompt string in the SDK's ``TextInput`` payload type.

        ``AsyncThread.run`` auto-normalizes strings, but ``AsyncThread.turn``
        takes typed ``Input`` only, so we construct the wrapper explicitly.
        """
        text_cls = self._sdk_syms.get("TextInput")
        if text_cls is None:
            return prompt
        try:
            return text_cls(prompt)
        except Exception:
            logger.debug("suppressed exception in _wrap_text_input", exc_info=True)
            return prompt

    # -- Thread lifecycle ----------------------------------------------------

    async def _resolve_thread(self) -> Any:
        """Resume an existing thread for the active session or start a new one."""
        client = self._sdk_client
        start_kwargs = self._build_thread_start_kwargs()
        thread_id = (
            self._thread_id_by_session.get(self._active_session, "")
            if self._active_session
            else ""
        )

        if thread_id:
            resume = getattr(client, "thread_resume", None) or getattr(
                client, "resume_thread", None
            )
            if resume is not None:
                return await self._maybe_await(resume(thread_id, **start_kwargs))

        start = getattr(client, "thread_start", None) or getattr(
            client, "start_thread", None
        )
        if start is None:
            msg = "Codex SDK client has no thread_start/start_thread method"
            raise RuntimeError(msg)
        thread = await self._maybe_await(start(**start_kwargs))
        tid = getattr(thread, "id", "") or ""
        if self._active_session and tid:
            self._thread_id_by_session[self._active_session] = tid
        return thread

    def _build_thread_start_kwargs(self) -> dict[str, Any]:
        """Assemble kwargs for ``Codex.thread_start``/``start_thread``."""
        kwargs: dict[str, Any] = {}
        if self._model:
            kwargs["model"] = self._model

        cwd = os.getcwd()
        if cwd:
            kwargs["cwd"] = cwd

        # Obscura handles approval/sandbox itself; tell Codex to never prompt.
        ask_cls = self._sdk_syms.get("AskForApproval")
        if ask_cls is not None:
            try:
                kwargs["approval_policy"] = ask_cls("never")
            except Exception:
                logger.debug(
                    "suppressed exception in _build_thread_start_kwargs", exc_info=True
                )
                try:
                    kwargs["approval_policy"] = ask_cls(root="never")
                except Exception:
                    logger.debug(
                        "suppressed exception in _build_thread_start_kwargs",
                        exc_info=True,
                    )

        sandbox_cls = self._sdk_syms.get("SandboxMode")
        if sandbox_cls is not None:
            try:
                kwargs["sandbox"] = sandbox_cls("workspace-write")
            except Exception:
                logger.debug(
                    "suppressed exception in _build_thread_start_kwargs", exc_info=True
                )

        system = self._build_system_prompt()
        if system:
            kwargs["developer_instructions"] = system

        return kwargs

    def _build_run_kwargs(self) -> dict[str, Any]:
        """Assemble kwargs for ``AsyncThread.run`` / ``AsyncThread.turn``."""
        kwargs: dict[str, Any] = {}
        effort_cls = self._sdk_syms.get("ReasoningEffort")
        if effort_cls is not None and self._reasoning_effort:
            try:
                kwargs["effort"] = effort_cls(self._reasoning_effort)
            except Exception:
                logger.debug("suppressed exception in _build_run_kwargs", exc_info=True)
        if self._model:
            kwargs["model"] = self._model
        return kwargs

    # -- Notification → StreamChunk mapping ----------------------------------

    def _map_notification_to_chunks(
        self,
        method: str,
        payload: Any,
    ) -> list[StreamChunk]:
        return map_notification_to_chunks(method, payload)

    # -- Content block helpers -----------------------------------------------

    def _items_to_content_blocks(
        self,
        items: list[Any],
        final_text: str,
    ) -> list[Any]:
        return items_to_content_blocks(items, final_text)

    # -- SDK bootstrapping ---------------------------------------------------

    def _import_sdk_class(self) -> type[Any]:
        """Import ``codex_app_server`` and cache the symbols we use."""
        try:
            mod = importlib.import_module(_SDK_MODULE)
        except ImportError as exc:
            py_exe = sys.executable
            msg = (
                "Official OpenAI Codex SDK not found. Install with: "
                f"`{py_exe} -m pip install "
                "openai-codex-app-server-sdk @ git+https://github.com/openai/codex.git#subdirectory=sdk/python`. "
                "See https://developers.openai.com/codex/sdk for details. "
                f"Import error: {exc}"
            )
            raise RuntimeError(msg) from exc

        sdk_cls = getattr(mod, "AsyncCodex", None)
        if not inspect.isclass(sdk_cls):
            msg = (
                f"{_SDK_MODULE} is installed but exposes no AsyncCodex class. "
                "Reinstall the official SDK from "
                "https://github.com/openai/codex (sdk/python)."
            )
            raise RuntimeError(msg)

        for sym in (
            "AppServerConfig",
            "AskForApproval",
            "SandboxMode",
            "ReasoningEffort",
            "TextInput",
        ):
            val = getattr(mod, sym, None)
            if val is not None:
                self._sdk_syms[sym] = val

        _relax_strict_response_models(mod)

        return sdk_cls

    async def _build_sdk_client(self, sdk_cls: type[Any]) -> Any:
        """Construct and initialize the SDK client."""
        codex_path = os.environ.get("OBSCURA_CODEX_PATH", "").strip() or shutil.which(
            "codex"
        )

        # Pass OpenAI credentials through to the spawned codex process.
        env: dict[str, str] = {}
        if self._auth.openai_api_key:
            env["OPENAI_API_KEY"] = self._auth.openai_api_key
            env["CODEX_API_KEY"] = self._auth.openai_api_key
        if self._auth.openai_base_url:
            env["OPENAI_BASE_URL"] = self._auth.openai_base_url

        config_cls = self._sdk_syms.get("AppServerConfig")
        client: Any
        if config_cls is not None:
            cfg_kwargs: dict[str, Any] = {}
            if codex_path:
                cfg_kwargs["codex_bin"] = codex_path
            cwd = os.getcwd()
            if cwd:
                cfg_kwargs["cwd"] = cwd
            if env:
                cfg_kwargs["env"] = env
            if self._mcp_servers:
                overrides = _mcp_servers_to_config_overrides(self._mcp_servers)
                if overrides:
                    cfg_kwargs["config_overrides"] = overrides
            try:
                config = config_cls(**cfg_kwargs)
                client = sdk_cls(config=config)
            except Exception:
                logger.debug("suppressed exception in _build_sdk_client", exc_info=True)
                client = sdk_cls()
        else:
            client = sdk_cls()

        # AsyncCodex initializes lazily via __aenter__; sync Codex initializes
        # in __init__. Support both and honor any ``start`` bootstrapping hook
        # used by test fakes.
        aenter = getattr(client, "__aenter__", None)
        if callable(aenter):
            client = await client.__aenter__()
        else:
            start = getattr(client, "start", None)
            if callable(start):
                await self._maybe_await(start())
        return client

    # -- Hooks ---------------------------------------------------------------

    async def _run_hooks(self, context: HookContext) -> None:
        callbacks = self._hooks.get(context.hook, [])
        for callback in callbacks:
            try:
                if inspect.iscoroutinefunction(callback):
                    await callback(context)
                else:
                    callback(context)
            except Exception:
                logger.debug("suppressed exception in _run_hooks", exc_info=True)
