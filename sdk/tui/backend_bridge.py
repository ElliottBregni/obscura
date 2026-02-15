"""
sdk.tui.backend_bridge -- Async bridge between Textual and ObscuraClient.

Manages the ObscuraClient lifecycle and routes streaming chunks to
TUI widget callbacks. Runs the client's async operations through
Textual's worker system to avoid event loop conflicts.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from sdk.internal.types import ChunkKind, Message, StreamChunk
from sdk.client import ObscuraClient


# ---------------------------------------------------------------------------
# Callback types
# ---------------------------------------------------------------------------

OnTextCallback = Callable[[str], None]
OnThinkingCallback = Callable[[str], None]
OnToolStartCallback = Callable[[str], None]
OnToolDeltaCallback = Callable[[str], None]
OnToolResultCallback = Callable[[str], None]
OnDoneCallback = Callable[[], None]
OnErrorCallback = Callable[[str], None]


# ---------------------------------------------------------------------------
# BackendBridge
# ---------------------------------------------------------------------------


class BackendBridge:
    """Manages ObscuraClient lifecycle and streams chunks to TUI widgets.

    The bridge holds a single ObscuraClient instance and provides
    methods to stream prompts with callback-based chunk routing.
    """

    def __init__(
        self,
        backend: str = "copilot",
        model: str | None = None,
        cwd: str | None = None,
        system_prompt: str = "",
    ) -> None:
        self._backend_name: str = backend
        self._model: str | None = model
        self._cwd: str | None = cwd
        self._system_prompt: str = system_prompt
        self._client: ObscuraClient | None = None
        self._connected: bool = False
        self._streaming: bool = False
        self._cancel_event: asyncio.Event = asyncio.Event()

        # Timing
        self._stream_start: float = 0.0
        self._last_duration: float = 0.0

    # -- Properties ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def is_connected(self) -> bool:
        """Alias for connected (used in tests/UI)."""
        return self._connected

    @property
    def streaming(self) -> bool:
        return self._streaming

    @property
    def backend_name(self) -> str:
        return self._backend_name

    @property
    def model(self) -> str | None:
        return self._model

    # Test/observability accessors
    @property
    def client(self) -> ObscuraClient | None:
        """Access the underlying client (testing/observability)."""
        return self._client

    @client.setter
    def client(self, value: ObscuraClient | None) -> None:
        self._client = value

    @property
    def last_duration(self) -> float:
        """Duration of the last stream in seconds."""
        return self._last_duration

    # -- Lifecycle ----------------------------------------------------------

    async def connect(self) -> None:
        """Initialize the ObscuraClient and connect to the backend.

        Raises:
            RuntimeError: If already connected.
            Exception: Backend connection errors.
        """
        if self._connected:
            return

        self._client = ObscuraClient(
            self._backend_name,
            model=self._model,
            system_prompt=self._system_prompt,
            cwd=self._cwd,
        )
        await self._client.start()
        self._connected = True

    async def disconnect(self) -> None:
        """Gracefully shut down the client."""
        if self._client:
            try:
                await self._client.stop()
            except Exception:
                pass
            finally:
                self._client = None
                self._connected = False

    async def reconnect(self) -> None:
        """Disconnect and reconnect (e.g., after backend switch)."""
        await self.disconnect()
        await self.connect()

    # -- Backend switching --------------------------------------------------

    async def switch_backend(
        self,
        backend: str,
        model: str | None = None,
    ) -> None:
        """Switch to a different backend.

        Args:
            backend: The new backend name ('claude' or 'copilot').
            model: Optional model override.
        """
        self._backend_name = backend
        if model is not None:
            self._model = model
        await self.reconnect()

    def update_system_prompt(self, prompt: str) -> None:
        """Update the system prompt for the next connection."""
        self._system_prompt = prompt

    # -- Streaming ----------------------------------------------------------

    async def stream_prompt(
        self,
        prompt: str,
        on_text: OnTextCallback | None = None,
        on_thinking: OnThinkingCallback | None = None,
        on_tool_start: OnToolStartCallback | None = None,
        on_tool_delta: OnToolDeltaCallback | None = None,
        on_tool_result: OnToolResultCallback | None = None,
        on_done: OnDoneCallback | None = None,
        on_error: OnErrorCallback | None = None,
    ) -> None:
        """Stream a prompt through ObscuraClient, dispatching chunks to callbacks.

        Routes StreamChunk events by kind:
        - TEXT_DELTA -> on_text(chunk.text)
        - THINKING_DELTA -> on_thinking(chunk.text)
        - TOOL_USE_START -> on_tool_start(chunk.tool_name)
        - TOOL_USE_DELTA -> on_tool_delta(chunk.tool_input_delta)
        - TOOL_RESULT -> on_tool_result(chunk.text)
        - DONE -> on_done()
        - ERROR -> on_error(chunk.text)

        The stream can be cancelled by calling ``cancel_stream()``.

        Args:
            prompt: The user prompt to send.
            on_text: Called for each text delta.
            on_thinking: Called for each thinking delta.
            on_tool_start: Called when a tool use begins.
            on_tool_delta: Called for tool input deltas.
            on_tool_result: Called when a tool result arrives.
            on_done: Called when streaming completes.
            on_error: Called on errors.
        """
        if not self._connected or not self._client:
            if on_error:
                on_error("Not connected to backend")
            return

        self._streaming = True
        self._cancel_event.clear()
        self._stream_start = time.monotonic()

        try:
            async for chunk in self._client.stream(prompt):
                # Check for cancellation
                if self._cancel_event.is_set():
                    break

                self._dispatch_chunk(
                    chunk,
                    on_text=on_text,
                    on_thinking=on_thinking,
                    on_tool_start=on_tool_start,
                    on_tool_delta=on_tool_delta,
                    on_tool_result=on_tool_result,
                    on_done=on_done,
                    on_error=on_error,
                )

                # Yield control so Textual can process widget repaints
                await asyncio.sleep(0)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            if on_error:
                on_error(str(e))
            return
        finally:
            self._last_duration = time.monotonic() - self._stream_start
            self._streaming = False

        # Call on_done for both normal completion and cancellation.
        # (Error case returns above and skips this.)
        if on_done:
            on_done()

    def _dispatch_chunk(
        self,
        chunk: StreamChunk,
        *,
        on_text: OnTextCallback | None,
        on_thinking: OnThinkingCallback | None,
        on_tool_start: OnToolStartCallback | None,
        on_tool_delta: OnToolDeltaCallback | None,
        on_tool_result: OnToolResultCallback | None,
        on_done: OnDoneCallback | None,
        on_error: OnErrorCallback | None,
    ) -> None:
        """Route a single chunk to the appropriate callback."""
        match chunk.kind:
            case ChunkKind.TEXT_DELTA:
                if on_text:
                    on_text(chunk.text)
            case ChunkKind.THINKING_DELTA:
                if on_thinking:
                    on_thinking(chunk.text)
            case ChunkKind.TOOL_USE_START:
                if on_tool_start:
                    on_tool_start(chunk.tool_name)
            case ChunkKind.TOOL_USE_DELTA:
                if on_tool_delta:
                    on_tool_delta(chunk.tool_input_delta)
            case ChunkKind.TOOL_RESULT:
                if on_tool_result:
                    on_tool_result(chunk.text)
            case ChunkKind.DONE:
                pass  # handled after the stream loop finishes
            case ChunkKind.ERROR:
                if on_error:
                    on_error(chunk.text)

    # -- Send (non-streaming) -----------------------------------------------

    async def send_prompt(self, prompt: str) -> Message:
        """Send a prompt and wait for the full response.

        Args:
            prompt: The user prompt to send.

        Returns:
            The complete Message response.

        Raises:
            RuntimeError: If not connected.
        """
        if not self._connected or not self._client:
            raise RuntimeError("Not connected to backend")

        start = time.monotonic()
        try:
            result = await self._client.send(prompt)
            return result
        finally:
            self._last_duration = time.monotonic() - start

    # -- Cancellation -------------------------------------------------------

    def cancel_stream(self) -> None:
        """Signal the current stream to stop."""
        self._cancel_event.set()

    # -- Context manager ----------------------------------------------------

    async def __aenter__(self) -> BackendBridge:
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.disconnect()
