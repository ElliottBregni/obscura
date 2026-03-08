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
import os
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
    ) -> None:
        self._auth = auth
        self._model = model or "gpt-5"
        self._system_prompt = system_prompt
        self._mcp_servers = mcp_servers or []

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

    # -- Provider Registry overrides -----------------------------------------

    async def list_models(self) -> list[RegistryModelInfo]:
        """List models available for Codex (minimal implementation)."""
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
        """Return the default model for Codex."""
        return "gpt-5"

    def validate_model(self, model_id: str) -> bool:
        """Check if a model ID is valid for Codex."""
        return True  # Minimal validation - Codex handles internally


    def native(self) -> NativeHandle:
        return NativeHandle(
            client=self._sdk_client,
            session=self._active_session,
            meta={"provider": self._sdk_module_name or "openai_codex_sdk", "model": self._model},
        )

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_calls=False,
            supports_tool_choice=False,
            supports_usage=False,
            supports_remote_sessions=True,
            supports_native_mode=True,
            native_features=("openai_codex_sdk", "sdk_threads"),
        )

    async def start(self) -> None:
        sdk_cls, module_name = self._import_sdk_class()
        self._sdk_client = self._build_sdk_client(sdk_cls, module_name)
        self._sdk_module_name = module_name
        self._started = True

    async def stop(self) -> None:
        self._started = False

    async def send(self, prompt: str, **kwargs: Any) -> Message:
        self._ensure_started()
        await self._run_hooks(
            HookContext(hook=HookPoint.USER_PROMPT_SUBMITTED, prompt=prompt)
        )
        thread = self._resolve_thread()
        run_kwargs: dict[str, Any] = {}
        if self._model:
            run_kwargs["model"] = self._model
        if self._system_prompt:
            run_kwargs["system_prompt"] = self._system_prompt
        # Preserve existing reasoning controls for GPT-family models.
        if "reasoning_effort" in kwargs:
            run_kwargs["reasoning_effort"] = kwargs["reasoning_effort"]
        elif isinstance(self._model, str) and self._model.lower().startswith("gpt-"):
            run_kwargs["reasoning_effort"] = "medium"

        turn = await self._run_thread(thread, prompt, run_kwargs)
        text = self._extract_turn_text(turn)
        if not text:
            raise RuntimeError("Codex Python SDK returned an empty response")

        thread_id = self._extract_thread_id(thread, turn)
        if self._active_session and thread_id:
            self._thread_by_session[self._active_session] = thread_id
            self._thread_obj_by_id[thread_id] = thread

        await self._run_hooks(HookContext(hook=HookPoint.STOP))
        return Message(
            role=Role.ASSISTANT,
            content=[ContentBlock(kind="text", text=text)],
            backend=Backend.CODEX,
        )

    async def stream(self, prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
        self._ensure_started()
        yield StreamChunk(kind=ChunkKind.MESSAGE_START)
        msg = await self.send(prompt, **kwargs)
        if msg.text:
            yield StreamChunk(kind=ChunkKind.TEXT_DELTA, text=msg.text)
        yield StreamChunk(
            kind=ChunkKind.DONE,
            metadata=StreamMetadata(finish_reason="stop", model_id=self._model),
        )

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

    def register_tool(self, spec: ToolSpec) -> None:
        self._tools.append(spec)
        self._tool_registry.register(spec)

    def register_hook(self, hook: HookPoint, callback: Callable[..., Any]) -> None:
        self._hooks[hook].append(callback)

    def get_tool_registry(self) -> ToolRegistry:
        return self._tool_registry

    def _ensure_started(self) -> None:
        if not self._started:
            raise RuntimeError("CodexBackend not started. Call start() first.")

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
            # Fall through to basic constructor for SDK variants without options.
            pass
        return sdk_cls()

    def _resolve_thread(self) -> Any:
        thread_options = self._make_thread_options()
        thread_id = (
            self._thread_by_session.get(self._active_session, "")
            if self._active_session
            else ""
        )
        if thread_id and thread_id in self._thread_obj_by_id:
            return self._thread_obj_by_id[thread_id]

        if thread_id and hasattr(self._sdk_client, "resume_thread"):
            if thread_options is not None:
                thread = self._sdk_client.resume_thread(thread_id, thread_options)
            else:
                thread = self._sdk_client.resume_thread(thread_id)
            self._thread_obj_by_id[thread_id] = thread
            return thread

        if thread_options is not None:
            thread = self._sdk_client.start_thread(thread_options)
        else:
            thread = self._sdk_client.start_thread()
        tid = self._extract_thread_id(thread, None)
        if tid:
            self._thread_obj_by_id[tid] = thread
            if self._active_session:
                self._thread_by_session[self._active_session] = tid
        return thread

    def _make_thread_options(self) -> Any | None:
        """Create SDK thread options that force skip_git_repo_check when supported."""
        # Both SDK variants can expose ThreadOptions off their module.
        # Explicitly set skip_git_repo_check to avoid trusted-repo failures when
        # working from untrusted directories.
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

        kwargs: dict[str, Any] = {}
        accepted: set[str] | None = None

        try:
            sig = inspect.signature(thread_opts_cls)
            accepted = set(sig.parameters)
            accepts_var_kw = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
        except (TypeError, ValueError):
            accepts_var_kw = True

        if self._model:
            kwargs["model"] = self._model

        cwd = os.getcwd()
        if cwd:
            if accepts_var_kw or accepted is None or "working_directory" in accepted:
                kwargs["working_directory"] = cwd
            elif "workingDirectory" in accepted:
                kwargs["workingDirectory"] = cwd

        if accepts_var_kw or accepted is None or "skip_git_repo_check" in accepted:
            kwargs["skip_git_repo_check"] = True
        elif "skipGitRepoCheck" in accepted:
            kwargs["skipGitRepoCheck"] = True

        try:
            return thread_opts_cls(**kwargs)
        except Exception:
            return None

    async def _run_thread(
        self,
        thread: Any,
        prompt: str,
        run_kwargs: dict[str, Any],
    ) -> Any:
        run = getattr(thread, "run", None)
        if run is None:
            raise RuntimeError("Codex thread object has no `run` method")

        filtered_kwargs: dict[str, Any] = {}
        try:
            sig = inspect.signature(run)
            params = sig.parameters
            accepts_var_kw = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
            if accepts_var_kw:
                filtered_kwargs = dict(run_kwargs)
            else:
                filtered_kwargs = {
                    k: v for k, v in run_kwargs.items() if k in params
                }
        except (TypeError, ValueError):
            filtered_kwargs = {}

        result = run(prompt, **filtered_kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return await asyncio.to_thread(lambda: result)

    @staticmethod
    def _extract_thread_id(thread: Any, turn: Any | None) -> str:
        for obj in (thread, turn):
            if obj is None:
                continue
            for key in ("id", "thread_id", "threadId"):
                val = getattr(obj, key, None)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return ""

    @staticmethod
    def _extract_turn_text(turn: Any) -> str:
        if turn is None:
            return ""
        for key in ("final_response", "output_text", "text", "response"):
            val = getattr(turn, key, None)
            if isinstance(val, str) and val.strip():
                return val.strip()
        if isinstance(turn, str):
            return turn.strip()
        if isinstance(turn, dict):
            for key in ("final_response", "output_text", "text", "response"):
                val = turn.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return ""

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
