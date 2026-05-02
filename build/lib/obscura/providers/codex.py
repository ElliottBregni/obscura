"""obscura.providers.codex — BackendProtocol implementation for the official Codex SDK.

This backend uses OpenAI's ``codex_app_server`` SDK (PyPI: ``codex-app-server-sdk``),
which drives the local ``codex`` binary via its ``app-server`` subcommand over
JSON-RPC. Reference: https://developers.openai.com/codex/sdk
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
from typing import TYPE_CHECKING, Any, cast

from obscura.core.sessions import SessionStore
from obscura.providers._tool_host import BackendToolHostMixin
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
)
from obscura.providers.registry import ModelInfo as RegistryModelInfo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from obscura.core.auth import AuthConfig


_SDK_MODULE = "codex_app_server"


# ---------------------------------------------------------------------------
# SDK ↔ CLI version-skew tolerance
# ---------------------------------------------------------------------------
#
# The pinned ``codex_app_server`` SDK declares some response fields as
# required that newer ``codex`` CLI binaries no longer emit (and vice
# versa). Notable cases:
#
#   * ``ThreadStartResponse.approvalsReviewer`` — added in the SDK after
#     it landed in CLI responses.
#   * ``Thread.ephemeral`` — present in the SDK's model but absent from
#     responses produced by ``codex-cli`` 0.106.x and earlier.
#
# We don't read these fields ourselves (we only need ``thread.id``), so
# we relax them to optional at SDK-import time. This is forward- and
# backward-compatible: when the CLI catches up, the field is populated
# normally; otherwise it defaults to ``None``.

_OPTIONAL_RELAXATIONS: dict[str, tuple[str, ...]] = {
    "ThreadStartResponse": ("approvals_reviewer",),
    "ThreadResumeResponse": ("approvals_reviewer",),
    "Thread": ("ephemeral",),
}


def _relax_strict_response_models(mod: Any) -> None:
    """Make selected SDK response fields optional to tolerate version skew.

    Scans the SDK's submodules for the model classes named in
    :data:`_OPTIONAL_RELAXATIONS` and downgrades the listed fields to
    ``default=None``. Silently no-ops on missing classes or fields so a
    future SDK rename doesn't blow up at startup.
    """
    candidates: list[Any] = [mod]
    generated = getattr(mod, "generated", None)
    if generated is not None:
        candidates.append(generated)
        v2 = getattr(generated, "v2_all", None)
        if v2 is not None:
            candidates.append(v2)

    for model_name, field_names in _OPTIONAL_RELAXATIONS.items():
        cls = next(
            (
                getattr(c, model_name)
                for c in candidates
                if hasattr(c, model_name)
                and hasattr(getattr(c, model_name), "model_fields")
            ),
            None,
        )
        if cls is None:
            continue
        changed = False
        for fname in field_names:
            field = cls.model_fields.get(fname)
            if field is None or not field.is_required():
                continue
            field.default = None
            changed = True
        if changed:
            try:
                cls.model_rebuild(force=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# MCP server → Codex config override translation
# ---------------------------------------------------------------------------
#
# The Codex CLI reads its MCP configuration from ``~/.codex/config.toml``
# (``[mcp_servers.<name>]`` tables). The ``codex_app_server`` SDK's
# ``AppServerConfig.config_overrides`` accepts an array of ``key.path=value``
# strings equivalent to ``codex -c key.path=value`` on the CLI, where each
# ``value`` is parsed as TOML (falling back to a raw string literal on
# parse failure).
#
# Obscura carries MCP server configs as a list of dicts; this helper
# translates that list into the override tuple Codex expects, so the
# backend can forward MCP servers that the CLI would otherwise only see
# from the on-disk config file. Used by :meth:`CodexBackend._build_sdk_client`.


def _mcp_servers_to_config_overrides(
    servers: list[dict[str, Any]],
) -> tuple[str, ...]:
    """Map Obscura's ``mcp_servers`` list to Codex ``-c`` override strings.

    Each server dict must carry a ``name``. Streamable-HTTP servers use
    ``url`` (and optionally ``bearer_token_env_var``); stdio servers use
    ``command`` (and optionally ``args``, ``env``). Entries missing a
    usable name are skipped.
    """
    overrides: list[str] = []
    for server in servers:
        name = str(server.get("name") or "").strip()
        if not name:
            continue
        key = _codex_config_key(name)

        url = server.get("url")
        if isinstance(url, str) and url:
            overrides.append(f"mcp_servers.{key}.url={_toml_str(url)}")
            bearer = server.get("bearer_token_env_var") or server.get(
                "bearer_token_env",
            )
            if isinstance(bearer, str) and bearer:
                overrides.append(
                    f"mcp_servers.{key}.bearer_token_env_var={_toml_str(bearer)}",
                )
            continue

        command = server.get("command")
        if isinstance(command, str) and command:
            overrides.append(f"mcp_servers.{key}.command={_toml_str(command)}")
            raw_args: Any = server.get("args")
            if isinstance(raw_args, list) and raw_args:
                args: list[Any] = cast("list[Any]", raw_args)
                overrides.append(
                    f"mcp_servers.{key}.args={_toml_string_array(args)}",
                )
            raw_env: Any = server.get("env")
            if isinstance(raw_env, dict) and raw_env:
                env_map: dict[str, Any] = cast("dict[str, Any]", raw_env)
                overrides.append(
                    f"mcp_servers.{key}.env={_toml_inline_table(env_map)}",
                )
    return tuple(overrides)


def _codex_config_key(name: str) -> str:
    """Sanitize a server name for use as a TOML dotted-path key.

    Dashes are legal in TOML bare keys but we normalize them to
    underscores so a single stable form reaches Codex regardless of how
    the caller wrote the name.
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _toml_str(value: str) -> str:
    """Serialize a Python string as a TOML basic string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_string_array(items: list[Any]) -> str:
    """Serialize a Python list as a TOML array of strings."""
    return "[" + ", ".join(_toml_str(str(x)) for x in items) + "]"


def _toml_inline_table(mapping: dict[str, Any]) -> str:
    """Serialize a Python dict as a TOML inline table of string values."""
    pairs = [
        f"{_codex_config_key(str(k))} = {_toml_str(str(v))}" for k, v in mapping.items()
    ]
    return "{ " + ", ".join(pairs) + " }"


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
        self._sdk_module_name = ""
        # SDK symbols cached at start() so we don't re-import on every turn.
        self._sdk_syms: dict[str, Any] = {}
        # The codex_app_server SDK rejects concurrent turn consumers on the
        # same client ("Concurrent turn consumers are not yet supported").
        # Obscura's REPL lets the user submit a new prompt while a turn is
        # still streaming, so we serialize calls to send()/stream() here —
        # the second prompt waits for the first turn's stream to drain.
        self._turn_lock = asyncio.Lock()

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

    def native(self) -> NativeHandle:
        return NativeHandle(
            client=self._sdk_client,
            session=self._active_session,
            meta={
                "provider": self._sdk_module_name or _SDK_MODULE,
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
        from obscura.integrations.mcp.discovery import (
            register_external_mcp_tools,
        )

        await register_external_mcp_tools(self, self._mcp_servers)

        sdk_cls, module_name = self._import_sdk_class()
        self._sdk_module_name = module_name
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
                pass
        close = getattr(client, "close", None)
        if callable(close):
            try:
                await self._maybe_await(close())
            except Exception:
                pass

    # -- Send / Stream -------------------------------------------------------

    async def send(self, prompt: str, **kwargs: Any) -> Message:
        """Send a prompt and wait for the full response."""
        self._ensure_started()
        await self._run_hooks(
            HookContext(hook=HookPoint.USER_PROMPT_SUBMITTED, prompt=prompt),
        )

        async with self._turn_lock:
            return await self._send_locked(prompt)

    async def _send_locked(self, prompt: str) -> Message:
        thread = await self._resolve_thread()
        run_kwargs = self._build_run_kwargs()
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

        # Hold the lock for the entire turn so that a second concurrent
        # caller waits here until our stream is fully drained, instead of
        # tripping the SDK's "concurrent turn consumers" guard.
        async with self._turn_lock:
            thread = await self._resolve_thread()
            run_kwargs = self._build_run_kwargs()
            wire_input = self._wrap_text_input(prompt)
            turn_handle = await self._maybe_await(
                thread.turn(wire_input, **run_kwargs),
            )

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
        """Sanitize tool name to match API pattern ^[a-zA-Z0-9_-]{1,128}$."""
        return re.sub(r"[^a-zA-Z0-9_-]", "_", name)[:128]

    @staticmethod
    def _unwrap_item(item: Any) -> Any:
        """Unwrap a pydantic RootModel wrapper (``ThreadItem.root``) if present."""
        if item is None:
            return None
        root = getattr(item, "root", None)
        return root if root is not None else item

    @staticmethod
    def _summarize_file_changes(item: Any) -> str:
        """Render a ``fileChange`` item's changes as a short comma-separated string."""
        raw: Any = getattr(item, "changes", None) or []
        parts: list[str] = []
        for change in raw:
            c: Any = change
            kind = getattr(c, "kind", None) or getattr(c, "type", "?")
            path = getattr(c, "path", "?")
            parts.append(f"{kind}:{path}")
        return ", ".join(parts)

    def _build_tool_listing(self) -> str:
        """Build a human-readable tool listing for the system prompt."""
        lines = ["## Available Tools", ""]
        lines.append(
            "You have the following tools. Use these EXACT names when calling tools:",
        )
        lines.append("")
        for spec in self._tools:
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
            from obscura.plugins.capabilities import build_capability_map_section

            cap_section = build_capability_map_section(self._tools)
            if cap_section:
                lines.append("")
                lines.append(cap_section)
        except Exception:
            pass
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
                client,
                "resume_thread",
                None,
            )
            if resume is not None:
                return await self._maybe_await(resume(thread_id, **start_kwargs))

        start = getattr(client, "thread_start", None) or getattr(
            client,
            "start_thread",
            None,
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
        """Assemble kwargs for ``AsyncCodex.thread_start``."""
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
                try:
                    kwargs["approval_policy"] = ask_cls(root="never")
                except Exception:
                    pass

        sandbox_cls = self._sdk_syms.get("SandboxMode")
        if sandbox_cls is not None:
            try:
                kwargs["sandbox"] = sandbox_cls("workspace-write")
            except Exception:
                pass

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
                pass
        if self._model:
            kwargs["model"] = self._model
        return kwargs

    # -- Notification → StreamChunk mapping ----------------------------------

    def _map_notification_to_chunks(
        self,
        method: str,
        payload: Any,
    ) -> list[StreamChunk]:
        """Map a Codex app-server notification to zero or more StreamChunks."""
        if payload is None:
            return []

        if method == "item/agentMessage/delta":
            delta = getattr(payload, "delta", "")
            if not delta:
                return []
            return [StreamChunk(kind=ChunkKind.TEXT_DELTA, text=delta, raw=payload)]

        if method in (
            "item/reasoning/textDelta",
            "item/reasoning/summaryTextDelta",
        ):
            delta = getattr(payload, "delta", "")
            if not delta:
                return []
            return [
                StreamChunk(kind=ChunkKind.THINKING_DELTA, text=delta, raw=payload),
            ]

        if method in ("item/started", "item/completed"):
            item = self._unwrap_item(getattr(payload, "item", None))
            if item is None:
                return []
            started = method == "item/started"
            item_type = getattr(item, "type", "")
            if item_type == "commandExecution":
                return self._command_execution_chunks(item, started=started)
            if item_type == "mcpToolCall":
                return self._mcp_tool_call_chunks(item, started=started)
            if item_type == "fileChange" and not started:
                return self._file_change_chunks(item)
            if item_type == "webSearch":
                return self._web_search_chunks(item, started=started)
            return []

        if method == "error":
            err = getattr(payload, "error", None)
            msg = getattr(err, "message", None) or "Unknown error"
            return [StreamChunk(kind=ChunkKind.ERROR, text=msg, raw=payload)]

        return []

    def _command_execution_chunks(
        self,
        item: Any,
        *,
        started: bool,
    ) -> list[StreamChunk]:
        item_id = getattr(item, "id", "") or ""
        if started:
            chunks = [
                StreamChunk(
                    kind=ChunkKind.TOOL_USE_START,
                    tool_name="shell_command",
                    tool_use_id=item_id,
                    raw=item,
                ),
            ]
            cmd = getattr(item, "command", "")
            if cmd:
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_DELTA,
                        tool_input_delta=json.dumps({"command": cmd}),
                        raw=item,
                    ),
                )
            return chunks

        output = getattr(item, "aggregated_output", "") or ""
        exit_code = getattr(item, "exit_code", None)
        text = output[:4096]
        if exit_code is not None:
            text = f"{text}\n[exit_code: {exit_code}]"
        return [
            StreamChunk(
                kind=ChunkKind.TOOL_RESULT,
                text=text,
                tool_use_id=item_id,
                raw=item,
            ),
            StreamChunk(
                kind=ChunkKind.TOOL_USE_END,
                tool_name="shell_command",
                tool_use_id=item_id,
                raw=item,
            ),
        ]

    def _mcp_tool_call_chunks(
        self,
        item: Any,
        *,
        started: bool,
    ) -> list[StreamChunk]:
        item_id = getattr(item, "id", "") or ""
        server = getattr(item, "server", "") or ""
        tool = getattr(item, "tool", "") or ""
        name = self._sanitize_tool_name(f"{server}_{tool}")
        if started:
            chunks: list[StreamChunk] = [
                StreamChunk(
                    kind=ChunkKind.TOOL_USE_START,
                    tool_name=name,
                    tool_use_id=item_id,
                    raw=item,
                ),
            ]
            args = getattr(item, "arguments", None)
            if args is not None:
                try:
                    args_str = args if isinstance(args, str) else json.dumps(args)
                except (TypeError, ValueError):
                    args_str = str(args)
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_DELTA,
                        tool_input_delta=args_str,
                        raw=item,
                    ),
                )
            return chunks

        error = getattr(item, "error", None)
        result_obj = getattr(item, "result", None)
        if error is not None:
            text = f"Error: {getattr(error, 'message', None) or error}"
        elif result_obj is not None:
            content = getattr(result_obj, "content", None)
            try:
                text = json.dumps(content) if content is not None else ""
            except (TypeError, ValueError):
                text = str(content)
        else:
            text = ""
        return [
            StreamChunk(
                kind=ChunkKind.TOOL_RESULT,
                text=text[:4096],
                tool_use_id=item_id,
                raw=item,
            ),
            StreamChunk(
                kind=ChunkKind.TOOL_USE_END,
                tool_name=name,
                tool_use_id=item_id,
                raw=item,
            ),
        ]

    def _file_change_chunks(self, item: Any) -> list[StreamChunk]:
        item_id = getattr(item, "id", "") or ""
        summary = self._summarize_file_changes(item)
        return [
            StreamChunk(
                kind=ChunkKind.TOOL_USE_START,
                tool_name="file_change",
                tool_use_id=item_id,
                raw=item,
            ),
            StreamChunk(
                kind=ChunkKind.TOOL_RESULT,
                text=summary,
                tool_use_id=item_id,
                raw=item,
            ),
            StreamChunk(
                kind=ChunkKind.TOOL_USE_END,
                tool_name="file_change",
                tool_use_id=item_id,
                raw=item,
            ),
        ]

    def _web_search_chunks(
        self,
        item: Any,
        *,
        started: bool,
    ) -> list[StreamChunk]:
        item_id = getattr(item, "id", "") or ""
        query = getattr(item, "query", "")
        if started:
            chunks = [
                StreamChunk(
                    kind=ChunkKind.TOOL_USE_START,
                    tool_name="web_search",
                    tool_use_id=item_id,
                    raw=item,
                ),
            ]
            if query:
                chunks.append(
                    StreamChunk(
                        kind=ChunkKind.TOOL_USE_DELTA,
                        tool_input_delta=json.dumps({"query": query}),
                        raw=item,
                    ),
                )
            return chunks
        return [
            StreamChunk(
                kind=ChunkKind.TOOL_USE_END,
                tool_name="web_search",
                tool_use_id=item_id,
                raw=item,
            ),
        ]

    # -- Content block helpers -----------------------------------------------

    def _items_to_content_blocks(
        self,
        items: list[Any],
        final_text: str,
    ) -> list[ContentBlock]:
        """Convert Codex thread items into Obscura ContentBlocks."""
        blocks: list[ContentBlock] = []
        has_text = False

        for raw in items:
            item = self._unwrap_item(raw)
            if item is None:
                continue
            item_type = getattr(item, "type", "")

            if item_type == "agentMessage":
                blocks.append(
                    ContentBlock(kind="text", text=getattr(item, "text", "") or ""),
                )
                has_text = True

            elif item_type == "reasoning":
                content = list(getattr(item, "content", None) or [])
                summary = list(getattr(item, "summary", None) or [])
                text = "\n".join(str(s) for s in content + summary)
                if text:
                    blocks.append(ContentBlock(kind="thinking", text=text))

            elif item_type == "commandExecution":
                blocks.append(
                    ContentBlock(
                        kind="tool_use",
                        tool_name="shell_command",
                        tool_input={"command": getattr(item, "command", "") or ""},
                        tool_use_id=getattr(item, "id", "") or "",
                    ),
                )

            elif item_type == "mcpToolCall":
                server = getattr(item, "server", "") or ""
                tool = getattr(item, "tool", "") or ""
                blocks.append(
                    ContentBlock(
                        kind="tool_use",
                        tool_name=self._sanitize_tool_name(f"{server}_{tool}"),
                        tool_input=getattr(item, "arguments", None) or {},
                        tool_use_id=getattr(item, "id", "") or "",
                    ),
                )

            elif item_type == "fileChange":
                blocks.append(
                    ContentBlock(
                        kind="tool_use",
                        tool_name="file_change",
                        tool_input={"changes": self._summarize_file_changes(item)},
                        tool_use_id=getattr(item, "id", "") or "",
                    ),
                )

        if not has_text:
            blocks.insert(0, ContentBlock(kind="text", text=final_text))

        return blocks

    # -- SDK bootstrapping ---------------------------------------------------

    def _import_sdk_class(self) -> tuple[type[Any], str]:
        """Import ``codex_app_server`` and cache the symbols we use."""
        try:
            mod = importlib.import_module(_SDK_MODULE)
        except ImportError as exc:
            py_exe = sys.executable
            msg = (
                "Official OpenAI Codex SDK not found. Install with: "
                f"`{py_exe} -m pip install codex-app-server-sdk`. "
                "See https://developers.openai.com/codex/sdk for details. "
                f"Import error: {exc}"
            )
            raise RuntimeError(msg) from exc

        sdk_cls = getattr(mod, "AsyncCodex", None) or getattr(mod, "Codex", None)
        if not inspect.isclass(sdk_cls):
            msg = (
                f"{_SDK_MODULE} is installed but exposes no AsyncCodex/Codex class. "
                "Expected codex-app-server-sdk >= 0.2.0."
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

        return sdk_cls, _SDK_MODULE

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
                if asyncio.iscoroutinefunction(callback):
                    await callback(context)
                else:
                    callback(context)
            except Exception:
                pass
