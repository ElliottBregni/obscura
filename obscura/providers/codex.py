"""
obscura.codex_backend -- BackendProtocol implementation for Python Codex SDK.

This backend uses the official OpenAI Codex SDK module:
- ``openai_codex_sdk``
- ``openai_codex`` (legacy module name)
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import re
import shutil
import sys
import uuid
from typing import Any, AsyncIterator, Callable

from obscura.core.auth import AuthConfig
from obscura.core.sessions import SessionStore
from obscura.core.tools import ToolRegistry
from obscura.core.types import (
    Backend,
    BackendCapabilities,
    ChunkKind,
    ContentBlock,
    HookContext,
    HookPoint,
    Message,
    NativeHandle,
    Role,
    SessionRef,
    StreamChunk,
    StreamMetadata,
    ToolSpec,
)
from obscura.providers.registry import ModelInfo as RegistryModelInfo


class CodexBackend:
    """BackendProtocol implementation using a Python Codex SDK."""

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
        self._model = model or "gpt-5"
        self._system_prompt = system_prompt
        self._mcp_servers = mcp_servers or []
        self._reasoning_effort = reasoning_effort or "medium"

        self._tools: list[ToolSpec] = []
        self._tool_registry = ToolRegistry()
        self._hooks: dict[HookPoint, list[Callable[..., Any]]] = {
            hp: [] for hp in HookPoint
        }
        self._session_store = SessionStore()
        self._active_session: str | None = None
        self._thread_by_session: dict[str, str] = {}
        self._thread_obj_by_id: dict[str, Any] = {}
        self._started = False

        self._sdk_client: Any = None
        self._sdk_module_name = ""

        # Delta tracking: SDK sends full accumulated text on item.updated,
        # not true deltas. Track chars already emitted per item ID.
        self._seen_text: dict[str, int] = {}

    # -- Provider Registry overrides -----------------------------------------

    async def list_models(self) -> list[RegistryModelInfo]:
        """List models available for Codex."""
        return [
            RegistryModelInfo(
                id="gpt-5",
                name="GPT-5 (Codex)",
                provider="codex",
                supports_tools=True,
                supports_vision=False,
            ),
        ]

    def get_default_model(self) -> str:
        return "gpt-5"

    def validate_model(self, model_id: str) -> bool:
        return True  # Codex validates internally

    def native(self) -> NativeHandle:
        return NativeHandle(
            client=self._sdk_client,
            session=self._active_session,
            meta={
                "provider": self._sdk_module_name or "openai_codex_sdk",
                "model": self._model,
            },
        )

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_calls=False,  # Codex manages its own tools autonomously
            supports_tool_choice=False,
            supports_usage=True,  # TurnCompletedEvent provides token counts
            supports_remote_sessions=True,
            supports_native_mode=True,
            native_features=("openai_codex_sdk", "sdk_threads", "autonomous_agent"),
        )

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        sdk_cls, module_name = self._import_sdk_class()
        self._sdk_client = self._build_sdk_client(sdk_cls, module_name)
        self._sdk_module_name = module_name
        self._started = True

    async def stop(self) -> None:
        self._started = False

    # -- Send / Stream -------------------------------------------------------

    async def send(self, prompt: str, **kwargs: Any) -> Message:
        """Send a prompt and wait for the full response."""
        self._ensure_started()
        await self._run_hooks(
            HookContext(hook=HookPoint.USER_PROMPT_SUBMITTED, prompt=prompt)
        )

        thread = self._resolve_thread()
        prepared = self._prepare_input(prompt)
        turn = await thread.run(prepared)

        text = turn.final_response
        if not text:
            # Fallback: extract from agent_message items
            for item in turn.items:
                if getattr(item, "type", "") == "agent_message":
                    text = getattr(item, "text", "")
                    if text:
                        break
        if not text:
            raise RuntimeError("Codex Python SDK returned an empty response")

        # Track thread/session mapping
        thread_id = getattr(thread, "id", "") or ""
        if self._active_session and thread_id:
            self._thread_by_session[self._active_session] = thread_id
            self._thread_obj_by_id[thread_id] = thread

        content_blocks = self._items_to_content_blocks(turn.items, text)

        await self._run_hooks(HookContext(hook=HookPoint.STOP))
        return Message(
            role=Role.ASSISTANT,
            content=content_blocks,
            backend=Backend.CODEX,
            model=self._model,
            session_id=self._active_session,
            raw=turn,
        )

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        """Send a prompt and yield real-time streaming chunks."""
        self._ensure_started()
        await self._run_hooks(
            HookContext(hook=HookPoint.USER_PROMPT_SUBMITTED, prompt=prompt)
        )

        # Clear delta tracking for this stream call
        self._seen_text.clear()

        thread = self._resolve_thread()
        prepared = self._prepare_input(prompt)
        streamed_turn = await thread.run_streamed(prepared)

        yield StreamChunk(kind=ChunkKind.MESSAGE_START)

        usage_data: dict[str, int] | None = None
        finish_reason = "stop"

        try:
            async for event in streamed_turn.events:
                event_type = getattr(event, "type", "")

                # Track thread_id from ThreadStartedEvent
                if event_type == "thread.started":
                    thread_id = getattr(event, "thread_id", "")
                    if self._active_session and thread_id:
                        self._thread_by_session[self._active_session] = thread_id
                        self._thread_obj_by_id[thread_id] = thread

                # Map event to StreamChunks
                for chunk in self._map_thread_event_to_chunks(event):
                    yield chunk

                # Extract usage from TurnCompletedEvent
                if event_type == "turn.completed":
                    u = getattr(event, "usage", None)
                    if u:
                        usage_data = {
                            "input_tokens": getattr(u, "input_tokens", 0),
                            "output_tokens": getattr(u, "output_tokens", 0),
                            "cached_input_tokens": getattr(
                                u, "cached_input_tokens", 0
                            ),
                        }

                # Track failures
                if event_type == "turn.failed":
                    finish_reason = "error"

        except Exception as exc:
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
            raise RuntimeError(f"Session {ref.session_id} not found")
        self._active_session = ref.session_id

    async def list_sessions(self) -> list[SessionRef]:
        return self._session_store.list_all(Backend.CODEX)

    async def delete_session(self, ref: SessionRef) -> None:
        self._session_store.remove(ref.session_id)
        thread_id = self._thread_by_session.pop(ref.session_id, None)
        if thread_id:
            self._thread_obj_by_id.pop(thread_id, None)
        if self._active_session == ref.session_id:
            self._active_session = None

    # -- Tools ---------------------------------------------------------------

    def register_tool(self, spec: ToolSpec) -> None:
        if any(t.name == spec.name for t in self._tools):
            return
        self._tools.append(spec)
        self._tool_registry.register(spec)

    def register_hook(self, hook: HookPoint, callback: Callable[..., Any]) -> None:
        self._hooks[hook].append(callback)

    def get_tool_registry(self) -> ToolRegistry:
        return self._tool_registry

    # -- Internals -----------------------------------------------------------

    def _ensure_started(self) -> None:
        if not self._started:
            raise RuntimeError("CodexBackend not started. Call start() first.")

    @staticmethod
    def _sanitize_tool_name(name: str) -> str:
        """Sanitize tool name to match API pattern ^[a-zA-Z0-9_-]{1,128}$."""
        return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:128]

    def _build_tool_listing(self) -> str:
        """Build a human-readable tool listing for the system prompt."""
        lines = ["## Available Tools", ""]
        lines.append(
            "You have the following tools. "
            "Use these EXACT names when calling tools:"
        )
        lines.append("")
        for spec in self._tools:
            desc = (spec.description or "").split("\n")[0][:120]
            lines.append(f"- `{self._sanitize_tool_name(spec.name)}`: {desc}")
        lines.append("")
        lines.append(
            "Do NOT invent tool names. "
            "If none of these tools fit, tell the user."
        )
        return "\n".join(lines)

    def _build_system_prompt(self) -> str:
        """Build full system prompt with tool listing appended."""
        prompt = self._system_prompt or ""
        if self._tools:
            tool_section = self._build_tool_listing()
            prompt = f"{prompt}\n\n{tool_section}" if prompt else tool_section
        return prompt

    def _prepare_input(self, prompt: str) -> str:
        """Prepend system context to user prompt.

        The Codex SDK's ThreadOptions has no system_prompt field, so we
        inject system instructions by prepending to the user input.
        """
        system = self._build_system_prompt()
        if system:
            return f"<system>\n{system}\n</system>\n\n{prompt}"
        return prompt

    # -- Event mapping -------------------------------------------------------

    def _map_thread_event_to_chunks(self, event: Any) -> list[StreamChunk]:
        """Map a Codex SDK ThreadEvent to zero or more StreamChunks."""
        chunks: list[StreamChunk] = []
        event_type = getattr(event, "type", "")

        # Item events carry ThreadItem payloads
        item = getattr(event, "item", None)
        if item is None:
            # ThreadErrorEvent
            if event_type == "error":
                msg = getattr(event, "message", "Unknown thread error")
                chunks.append(StreamChunk(kind=ChunkKind.ERROR, text=msg, raw=event))
            return chunks

        item_type = getattr(item, "type", "")

        if item_type == "agent_message":
            text = getattr(item, "text", "")
            item_id = getattr(item, "id", "")
            if text and event_type in ("item.updated", "item.completed"):
                # Emit only the new delta (SDK sends full accumulated text)
                prev_len = self._seen_text.get(item_id, 0)
                delta = text[prev_len:]
                if delta:
                    self._seen_text[item_id] = len(text)
                    chunks.append(
                        StreamChunk(
                            kind=ChunkKind.TEXT_DELTA,
                            text=delta,
                            raw=item,
                            native_event=event,
                        )
                    )

        elif item_type == "reasoning":
            text = getattr(item, "text", "")
            item_id = getattr(item, "id", "")
            if text and event_type in ("item.updated", "item.completed"):
                prev_len = self._seen_text.get(item_id, 0)
                delta = text[prev_len:]
                if delta:
                    self._seen_text[item_id] = len(text)
                    chunks.append(
                        StreamChunk(
                            kind=ChunkKind.THINKING_DELTA,
                            text=delta,
                            raw=item,
                            native_event=event,
                        )
                    )

        elif item_type == "command_execution":
            item_id = getattr(item, "id", "")
            if event_type == "item.started":
                cmd = getattr(item, "command", "")
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_START,
                        tool_name="shell_command",
                        tool_use_id=item_id,
                        raw=item,
                        native_event=event,
                    )
                )
                if cmd:
                    chunks.append(
                        StreamChunk(
                            kind=ChunkKind.TOOL_USE_DELTA,
                            tool_input_delta=json.dumps({"command": cmd}),
                            raw=item,
                            native_event=event,
                        )
                    )
            elif event_type == "item.completed":
                output = getattr(item, "aggregated_output", "")
                exit_code = getattr(item, "exit_code", None)
                result_text = output[:4096]
                if exit_code is not None:
                    result_text += f"\n[exit_code: {exit_code}]"
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_RESULT,
                        text=result_text,
                        tool_use_id=item_id,
                        raw=item,
                        native_event=event,
                    )
                )
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_END,
                        tool_name="shell_command",
                        tool_use_id=item_id,
                        raw=item,
                        native_event=event,
                    )
                )

        elif item_type == "mcp_tool_call":
            item_id = getattr(item, "id", "")
            server = getattr(item, "server", "")
            tool = getattr(item, "tool", "")
            tool_name = self._sanitize_tool_name(f"{server}_{tool}")
            if event_type == "item.started":
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_START,
                        tool_name=tool_name,
                        tool_use_id=item_id,
                        raw=item,
                        native_event=event,
                    )
                )
                args = getattr(item, "arguments", None)
                if args:
                    args_str = (
                        json.dumps(args) if not isinstance(args, str) else args
                    )
                    chunks.append(
                        StreamChunk(
                            kind=ChunkKind.TOOL_USE_DELTA,
                            tool_input_delta=args_str,
                            raw=item,
                            native_event=event,
                        )
                    )
            elif event_type == "item.completed":
                result_text = ""
                error_obj = getattr(item, "error", None)
                result_obj = getattr(item, "result", None)
                if error_obj:
                    result_text = f"Error: {getattr(error_obj, 'message', str(error_obj))}"
                elif result_obj:
                    content = getattr(result_obj, "content", [])
                    result_text = json.dumps(content) if content else ""
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_RESULT,
                        text=result_text[:4096],
                        tool_use_id=item_id,
                        raw=item,
                        native_event=event,
                    )
                )
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_END,
                        tool_name=tool_name,
                        tool_use_id=item_id,
                        raw=item,
                        native_event=event,
                    )
                )

        elif item_type == "file_change":
            item_id = getattr(item, "id", "")
            if event_type == "item.completed":
                changes = getattr(item, "changes", [])
                summary = ", ".join(
                    f"{getattr(c, 'kind', '?')}:{getattr(c, 'path', '?')}"
                    for c in changes
                )
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_START,
                        tool_name="file_change",
                        tool_use_id=item_id,
                        raw=item,
                        native_event=event,
                    )
                )
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_RESULT,
                        text=summary,
                        tool_use_id=item_id,
                        raw=item,
                        native_event=event,
                    )
                )
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_END,
                        tool_name="file_change",
                        tool_use_id=item_id,
                        raw=item,
                        native_event=event,
                    )
                )

        elif item_type == "web_search":
            item_id = getattr(item, "id", "")
            if event_type in ("item.started", "item.completed"):
                query = getattr(item, "query", "")
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_START,
                        tool_name="web_search",
                        tool_use_id=item_id,
                        raw=item,
                        native_event=event,
                    )
                )
                if query:
                    chunks.append(
                        StreamChunk(
                            kind=ChunkKind.TOOL_USE_DELTA,
                            tool_input_delta=json.dumps({"query": query}),
                            raw=item,
                            native_event=event,
                        )
                    )
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_END,
                        tool_name="web_search",
                        tool_use_id=item_id,
                        raw=item,
                        native_event=event,
                    )
                )

        elif item_type == "error":
            msg = getattr(item, "message", "Unknown error")
            chunks.append(
                StreamChunk(
                    kind=ChunkKind.ERROR,
                    text=msg,
                    raw=item,
                    native_event=event,
                )
            )

        return chunks

    # -- Content block helpers -----------------------------------------------

    def _items_to_content_blocks(
        self, items: list[Any], final_text: str
    ) -> list[ContentBlock]:
        """Convert Codex Turn items into Obscura ContentBlocks."""
        blocks: list[ContentBlock] = []
        has_text = False

        for item in items:
            item_type = getattr(item, "type", "")

            if item_type == "agent_message":
                blocks.append(
                    ContentBlock(kind="text", text=getattr(item, "text", ""))
                )
                has_text = True

            elif item_type == "reasoning":
                blocks.append(
                    ContentBlock(kind="thinking", text=getattr(item, "text", ""))
                )

            elif item_type == "command_execution":
                blocks.append(
                    ContentBlock(
                        kind="tool_use",
                        tool_name="shell_command",
                        tool_input={"command": getattr(item, "command", "")},
                        tool_use_id=getattr(item, "id", ""),
                    )
                )

            elif item_type == "mcp_tool_call":
                server = getattr(item, "server", "")
                tool = getattr(item, "tool", "")
                blocks.append(
                    ContentBlock(
                        kind="tool_use",
                        tool_name=self._sanitize_tool_name(f"{server}_{tool}"),
                        tool_input=getattr(item, "arguments", {}) or {},
                        tool_use_id=getattr(item, "id", ""),
                    )
                )

            elif item_type == "file_change":
                changes = getattr(item, "changes", [])
                summary = ", ".join(
                    f"{getattr(c, 'kind', '?')}:{getattr(c, 'path', '?')}"
                    for c in changes
                )
                blocks.append(
                    ContentBlock(
                        kind="tool_use",
                        tool_name="file_change",
                        tool_input={"changes": summary},
                        tool_use_id=getattr(item, "id", ""),
                    )
                )

            elif item_type == "error":
                blocks.append(
                    ContentBlock(
                        kind="text",
                        text=f"[Error] {getattr(item, 'message', '')}",
                        is_error=True,
                    )
                )

        # Ensure we have at least one text block
        if not has_text:
            blocks.insert(0, ContentBlock(kind="text", text=final_text))

        return blocks

    # -- Thread management ---------------------------------------------------

    def _resolve_thread(self) -> Any:
        """Resolve or create a thread for the current session."""
        thread_options = self._make_thread_options()
        thread_id = (
            self._thread_by_session.get(self._active_session, "")
            if self._active_session
            else ""
        )

        # Reuse cached thread object
        if thread_id and thread_id in self._thread_obj_by_id:
            return self._thread_obj_by_id[thread_id]

        # Resume existing thread
        if thread_id and hasattr(self._sdk_client, "resume_thread"):
            thread = self._sdk_client.resume_thread(thread_id, thread_options)
            self._thread_obj_by_id[thread_id] = thread
            return thread

        # Start new thread
        thread = self._sdk_client.start_thread(thread_options)
        tid = getattr(thread, "id", "") or ""
        if tid:
            self._thread_obj_by_id[tid] = thread
            if self._active_session:
                self._thread_by_session[self._active_session] = tid
        return thread

    def _make_thread_options(self) -> Any:
        """Create ThreadOptions with full capability configuration."""
        mod_name = self._sdk_module_name
        if not mod_name:
            return None
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            return None

        thread_opts_cls = getattr(mod, "ThreadOptions", None)
        if thread_opts_cls is None:
            return None

        kwargs: dict[str, Any] = {
            "skip_git_repo_check": True,
            "approval_policy": "never",
            "network_access_enabled": True,
            "web_search_enabled": True,
        }

        if self._model:
            kwargs["model"] = self._model

        cwd = os.getcwd()
        if cwd:
            kwargs["working_directory"] = cwd

        if self._reasoning_effort:
            kwargs["model_reasoning_effort"] = self._reasoning_effort

        try:
            return thread_opts_cls(**kwargs)
        except Exception:
            # Fallback: try without newer fields for older SDK versions
            try:
                return thread_opts_cls(
                    model=self._model,
                    working_directory=cwd or None,
                    skip_git_repo_check=True,
                )
            except Exception:
                return None

    # -- SDK bootstrapping ---------------------------------------------------

    def _import_sdk_class(self) -> tuple[type[Any], str]:
        module_candidates = ("openai_codex_sdk", "openai_codex")
        class_candidates = ("Codex", "CodexClient", "Client")
        import_errors: list[str] = []

        for mod_name in module_candidates:
            try:
                mod = importlib.import_module(mod_name)
            except ImportError as exc:
                import_errors.append(f"{mod_name}: {exc}")
                continue

            for cls_name in class_candidates:
                sdk_cls = getattr(mod, cls_name, None)
                if inspect.isclass(sdk_cls):
                    return sdk_cls, mod_name

        py_exe = sys.executable
        raise RuntimeError(
            "Official OpenAI Codex SDK not found or invalid. Install with: "
            f"`{py_exe} -m pip install openai-codex-sdk` "
            "(or run via your project environment, e.g. `uv run obscura ...`). "
            f"Tried modules: {', '.join(module_candidates)}. "
            f"Import errors: {'; '.join(import_errors) or 'none'}."
        )

    def _build_sdk_client(self, sdk_cls: type[Any], module_name: str) -> Any:
        """Construct SDK client, forcing codex binary path when supported."""
        codex_path = os.environ.get("OBSCURA_CODEX_PATH", "").strip() or shutil.which(
            "codex"
        )
        try:
            mod = importlib.import_module(module_name)
            options_cls = getattr(mod, "CodexOptions", None)
            if options_cls is not None:
                kwargs: dict[str, Any] = {}
                if codex_path:
                    kwargs["codex_path_override"] = codex_path
                if self._auth.openai_base_url:
                    kwargs["base_url"] = self._auth.openai_base_url
                if self._auth.openai_api_key:
                    kwargs["api_key"] = self._auth.openai_api_key
                opts = options_cls(**kwargs)
                return sdk_cls(opts)
        except Exception:
            pass
        return sdk_cls()

    # -- Hooks ---------------------------------------------------------------

    async def _run_hooks(self, context: HookContext) -> None:
        callbacks = self._hooks.get(context.hook, [])
        for callback in callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(context)
                else:
                    callback(context)
            except Exception:
                pass
