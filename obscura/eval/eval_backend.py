"""Lightweight backends for eval execution.

Two implementations:

* ``AnthropicEvalBackend`` — calls the Anthropic Messages API directly
  via the ``anthropic`` Python SDK.  Requires ``ANTHROPIC_API_KEY``.
* ``ClaudeCliEvalBackend`` — shells out to the ``claude`` CLI binary
  (``claude -p --output-format stream-json``).  Authenticates via the
  CLI's built-in OAuth flow so it works with ``CLAUDE_CODE_OAUTH_TOKEN``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import uuid
from typing import TYPE_CHECKING, Any

from obscura.core.tools import ToolRegistry
from obscura.core.types import (
    Backend,
    BackendCapabilities,
    ChunkKind,
    ContentBlock,
    HookPoint,
    Message,
    NativeHandle,
    Role,
    SessionRef,
    StreamChunk,
    StreamMetadata,
    ToolSpec,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_STUB_HOOKS: dict[HookPoint, list[Any]] = {hp: [] for hp in HookPoint}


def _make_hooks() -> dict[HookPoint, list[Any]]:
    return {hp: [] for hp in HookPoint}


# ---------------------------------------------------------------------------
# AnthropicEvalBackend — uses the ``anthropic`` Python SDK
# ---------------------------------------------------------------------------


class AnthropicEvalBackend:
    """Minimal ``BackendProtocol`` implementation using the Anthropic Messages API.

    Designed for eval engine use — streams tool calls and text without
    requiring the Claude Agent SDK subprocess.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "claude-sonnet-4-5-20250929",
        auth_token: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._auth_token = auth_token
        self._model = model
        self._registry = ToolRegistry()
        self._hooks: dict[HookPoint, list[Any]] = _make_hooks()
        self._client: Any = None
        self._conversations: dict[str, list[dict[str, Any]]] = {}

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        import anthropic

        kwargs: dict[str, Any] = {}
        if self._auth_token:
            kwargs["auth_token"] = self._auth_token
        else:
            kwargs["api_key"] = self._api_key
        self._client = anthropic.AsyncAnthropic(**kwargs)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # -- streaming -----------------------------------------------------------

    async def stream(  # noqa: C901
        self,
        prompt: str,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        if self._client is None:
            await self.start()

        session_id: str = kwargs.get("session_id", "")
        history = self._conversations.setdefault(session_id, [])

        tools = self._build_tools()

        messages = list(history)
        messages.append({"role": "user", "content": prompt})

        create_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 4096,
            "messages": messages,
        }
        if tools:
            create_kwargs["tools"] = tools

        yield StreamChunk(kind=ChunkKind.MESSAGE_START)

        try:
            async with self._client.messages.stream(**create_kwargs) as stream:
                async for event in stream:
                    event_type = event.type

                    if event_type == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            yield StreamChunk(
                                kind=ChunkKind.TOOL_USE_START,
                                tool_name=block.name,
                                tool_use_id=block.id,
                            )
                    elif event_type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield StreamChunk(
                                kind=ChunkKind.TEXT_DELTA,
                                text=delta.text,
                            )
                        elif delta.type == "input_json_delta":
                            yield StreamChunk(
                                kind=ChunkKind.TOOL_USE_DELTA,
                                tool_input_delta=delta.partial_json,
                            )

                final_message = stream.get_final_message()

            history.append({"role": "user", "content": prompt})
            assistant_content: list[dict[str, Any]] = []
            for block in final_message.content:
                if block.type == "text":
                    assistant_content.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        },
                    )
                    yield StreamChunk(
                        kind=ChunkKind.TOOL_USE_END,
                        tool_name=block.name,
                        tool_use_id=block.id,
                        tool_input_delta=json.dumps(block.input),
                    )
            history.append({"role": "assistant", "content": assistant_content})

        except Exception as exc:
            logger.exception("Anthropic API error: %s", exc)
            yield StreamChunk(kind=ChunkKind.ERROR, text=str(exc))

        yield StreamChunk(
            kind=ChunkKind.DONE,
            metadata=StreamMetadata(model_id=self._model, finish_reason="end_turn"),
        )

    # -- send ----------------------------------------------------------------

    async def send(self, prompt: str, **kwargs: Any) -> Message:
        content_blocks: list[ContentBlock] = []
        text_parts: list[str] = []

        async for chunk in self.stream(prompt, **kwargs):
            if chunk.kind == ChunkKind.TEXT_DELTA:
                text_parts.append(chunk.text)
            elif chunk.kind == ChunkKind.TOOL_USE_START:
                content_blocks.append(
                    ContentBlock(
                        kind="tool_use",
                        tool_name=chunk.tool_name,
                        tool_use_id=chunk.tool_use_id,
                    ),
                )

        if text_parts:
            content_blocks.insert(
                0,
                ContentBlock(kind="text", text="".join(text_parts)),
            )

        return Message(role=Role.ASSISTANT, content=content_blocks, model=self._model)

    # -- sessions (stub) -----------------------------------------------------

    async def create_session(self, **kwargs: Any) -> SessionRef:
        sid = f"eval-{uuid.uuid4().hex[:12]}"
        self._conversations[sid] = []
        return SessionRef(session_id=sid, backend=Backend.CLAUDE)

    async def resume_session(self, ref: SessionRef) -> None:
        pass

    async def list_sessions(self) -> list[SessionRef]:
        return [
            SessionRef(session_id=sid, backend=Backend.CLAUDE)
            for sid in self._conversations
        ]

    async def delete_session(self, ref: SessionRef) -> None:
        self._conversations.pop(ref.session_id, None)

    # -- tools & hooks -------------------------------------------------------

    def register_tool(self, spec: ToolSpec) -> None:
        if spec.name not in {s.name for s in self._registry.all()}:
            self._registry.register(spec)

    def register_hook(self, hook: HookPoint, callback: Any) -> None:
        self._hooks[hook].append(callback)

    def get_tool_registry(self) -> ToolRegistry:
        return self._registry

    # -- metadata ------------------------------------------------------------

    @property
    def native(self) -> NativeHandle:
        return NativeHandle(client=self._client)

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_calls=True,
            supports_tool_choice=True,
            supports_usage=True,
        )

    # -- internal ------------------------------------------------------------

    def _build_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for spec in self._registry.all():
            tools.append(
                {
                    "name": spec.name,
                    "description": spec.description,
                    "input_schema": spec.parameters,
                },
            )
        return tools


# ---------------------------------------------------------------------------
# ClaudeCliEvalBackend — uses ``claude -p`` subprocess (OAuth-compatible)
# ---------------------------------------------------------------------------


class ClaudeCliEvalBackend:
    """``BackendProtocol`` implementation that shells out to the ``claude`` CLI.

    Uses ``claude -p --output-format stream-json --verbose`` to get a JSON
    event stream.  The CLI handles OAuth authentication internally, so this
    backend works with ``CLAUDE_CODE_OAUTH_TOKEN`` without needing a raw
    Anthropic API key.
    """

    def __init__(self, *, model: str | None = None, max_turns: int = 10) -> None:
        self._model = model
        self._max_turns = max_turns
        self._registry = ToolRegistry()
        self._hooks: dict[HookPoint, list[Any]] = _make_hooks()
        self._cli_path: str | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self._cli_path = shutil.which("claude")
        if not self._cli_path:
            msg = "claude CLI not found on PATH. Install Claude Code to use OAuth eval."
            raise RuntimeError(
                msg,
            )

    async def stop(self) -> None:
        pass

    # -- streaming -----------------------------------------------------------

    async def stream(  # noqa: C901
        self,
        prompt: str,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        if self._cli_path is None:
            await self.start()

        max_turns = kwargs.get("max_turns", self._max_turns)

        cmd = [
            self._cli_path or "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            str(max_turns),
            "--dangerously-skip-permissions",
        ]
        if self._model:
            cmd.extend(["--model", self._model])

        yield StreamChunk(kind=ChunkKind.MESSAGE_START)

        model_id = self._model or ""
        text_parts: list[str] = []

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=4 * 1024 * 1024,  # 4 MB — CLI init events can be large
            )

            assert proc.stdout is not None  # noqa: S101
            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type", "")

                if event_type == "assistant":
                    msg = event.get("message", {})
                    model_id = msg.get("model", model_id)
                    for block in msg.get("content", []):
                        if block.get("type") == "text":
                            yield StreamChunk(
                                kind=ChunkKind.TEXT_DELTA,
                                text=block["text"],
                            )
                            text_parts.append(block["text"])
                        elif block.get("type") == "tool_use":
                            tool_name = block.get("name", "")
                            tool_id = block.get("id", "")
                            tool_input = block.get("input", {})
                            yield StreamChunk(
                                kind=ChunkKind.TOOL_USE_START,
                                tool_name=tool_name,
                                tool_use_id=tool_id,
                            )
                            yield StreamChunk(
                                kind=ChunkKind.TOOL_USE_DELTA,
                                tool_input_delta=json.dumps(tool_input),
                                tool_use_id=tool_id,
                            )
                            yield StreamChunk(
                                kind=ChunkKind.TOOL_USE_END,
                                tool_name=tool_name,
                                tool_use_id=tool_id,
                                tool_input_delta=json.dumps(tool_input),
                            )

                elif event_type == "tool_use":
                    tool_name = event.get("tool_name", event.get("name", ""))
                    tool_id = event.get("tool_use_id", "")
                    tool_input = event.get("input", event.get("tool_input", {}))
                    yield StreamChunk(
                        kind=ChunkKind.TOOL_USE_START,
                        tool_name=tool_name,
                        tool_use_id=tool_id,
                    )
                    yield StreamChunk(
                        kind=ChunkKind.TOOL_USE_DELTA,
                        tool_input_delta=json.dumps(tool_input),
                        tool_use_id=tool_id,
                    )
                    yield StreamChunk(
                        kind=ChunkKind.TOOL_USE_END,
                        tool_name=tool_name,
                        tool_use_id=tool_id,
                        tool_input_delta=json.dumps(tool_input),
                    )

                elif event_type == "tool_result":
                    yield StreamChunk(
                        kind=ChunkKind.TOOL_RESULT,
                        text=str(event.get("output", "")),
                    )

                elif event_type == "result":
                    # Final result — extract any remaining text
                    result_text = event.get("result", "")
                    if result_text and result_text not in "".join(text_parts):
                        yield StreamChunk(
                            kind=ChunkKind.TEXT_DELTA,
                            text=result_text,
                        )

            await proc.wait()

        except Exception as exc:
            logger.exception("Claude CLI error: %s", exc)
            yield StreamChunk(kind=ChunkKind.ERROR, text=str(exc))

        yield StreamChunk(
            kind=ChunkKind.DONE,
            metadata=StreamMetadata(model_id=model_id, finish_reason="end_turn"),
        )

    # -- send ----------------------------------------------------------------

    async def send(self, prompt: str, **kwargs: Any) -> Message:
        content_blocks: list[ContentBlock] = []
        text_parts: list[str] = []

        async for chunk in self.stream(prompt, **kwargs):
            if chunk.kind == ChunkKind.TEXT_DELTA:
                text_parts.append(chunk.text)
            elif chunk.kind == ChunkKind.TOOL_USE_START:
                content_blocks.append(
                    ContentBlock(
                        kind="tool_use",
                        tool_name=chunk.tool_name,
                        tool_use_id=chunk.tool_use_id,
                    ),
                )

        if text_parts:
            content_blocks.insert(
                0,
                ContentBlock(kind="text", text="".join(text_parts)),
            )

        return Message(
            role=Role.ASSISTANT,
            content=content_blocks,
            model=self._model or "",
        )

    # -- sessions (stub) -----------------------------------------------------

    async def create_session(self, **kwargs: Any) -> SessionRef:
        return SessionRef(
            session_id=f"cli-{uuid.uuid4().hex[:12]}",
            backend=Backend.CLAUDE,
        )

    async def resume_session(self, ref: SessionRef) -> None:
        pass

    async def list_sessions(self) -> list[SessionRef]:
        return []

    async def delete_session(self, ref: SessionRef) -> None:
        pass

    # -- tools & hooks -------------------------------------------------------

    def register_tool(self, spec: ToolSpec) -> None:
        if spec.name not in {s.name for s in self._registry.all()}:
            self._registry.register(spec)

    def register_hook(self, hook: HookPoint, callback: Any) -> None:
        self._hooks[hook].append(callback)

    def get_tool_registry(self) -> ToolRegistry:
        return self._registry

    # -- metadata ------------------------------------------------------------

    @property
    def native(self) -> NativeHandle:
        return NativeHandle()

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_calls=True,
        )
