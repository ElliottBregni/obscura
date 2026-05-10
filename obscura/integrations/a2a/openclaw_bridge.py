"""obscura.a2a.openclaw_bridge — Adapter bridging A2A protocol calls to OpenClaw.

OpenClaw speaks OpenAI-compatible chat completions (POST /v1/chat/completions)
but does not implement A2A natively.  This module provides an
:class:`OpenClawBridge` that:

1. Accepts an inbound A2A ``message/send`` payload (text extracted from the
   first ``TextPart`` in the message).
2. Translates it into an OpenAI-style chat completions request directed at
   the OpenClaw gateway.
3. Packages the response text back into an :class:`~obscura.core.models.a2a.A2ATask`
   in the ``completed`` state so callers get a standard A2A result.

The bridge is intentionally thin — it handles the most common single-turn
text case.  Streaming and multi-turn are left as future work once OpenClaw
exposes SSE on its completions endpoint.

Usage::

    bridge = OpenClawBridge.from_config(
        token="4a30d783737e2aac23148de52a29d9b820cffba3eda8754a",
        gateway_url="http://localhost:18789",
    )
    async with bridge:
        task = await bridge.send("What is the capital of France?")
        print(task.status.state)   # completed
        print(task.artifacts[0].parts[0].text)  # "Paris …"
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from collections.abc import AsyncGenerator

import httpx

from obscura.core.enums.protocol import A2ARole, A2ATaskState
from obscura.core.models.a2a import (
    A2AMessage,
    A2AStatusUpdateEvent,
    A2ATask,
    A2ATaskStatus,
    Artifact,
    TextPart,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_GATEWAY_URL = "http://localhost:18789"
_DEFAULT_MODEL = "openclaw/main"
_DEFAULT_TIMEOUT = 120.0

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class OpenClawBridgeConfig:
    """Runtime configuration for :class:`OpenClawBridge`.

    Parameters
    ----------
    token:
        Bearer token accepted by the OpenClaw gateway.
    gateway_url:
        Base URL of the OpenClaw HTTP gateway.
    model:
        Chat completions model identifier.  Defaults to ``"openclaw/main"``.
    timeout:
        HTTP request timeout in seconds.

    """

    token: str
    gateway_url: str = _DEFAULT_GATEWAY_URL
    model: str = _DEFAULT_MODEL
    timeout: float = _DEFAULT_TIMEOUT
    extra_headers: dict[str, str] = field(default_factory=dict[str, str])


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class OpenClawBridge:
    """Translates A2A ``message/send`` calls into OpenClaw chat completions.

    Parameters
    ----------
    config:
        :class:`OpenClawBridgeConfig` holding token, URL, and model.

    """

    def __init__(self, config: OpenClawBridgeConfig) -> None:
        self._config = config
        self._http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        token: str,
        gateway_url: str = _DEFAULT_GATEWAY_URL,
        *,
        model: str = _DEFAULT_MODEL,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> "OpenClawBridge":
        """Construct a bridge from explicit parameters.

        Parameters
        ----------
        token:
            OpenClaw bearer token.
        gateway_url:
            Base URL of the OpenClaw gateway (default: ``http://localhost:18789``).
        model:
            Completions model (default: ``"openclaw/main"``).
        timeout:
            HTTP timeout in seconds.

        """
        return cls(
            OpenClawBridgeConfig(
                token=token,
                gateway_url=gateway_url.rstrip("/"),
                model=model,
                timeout=timeout,
            )
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Create the underlying HTTP client."""
        headers: dict[str, str] = {
            "Authorization": f"Bearer {self._config.token}",
            "Content-Type": "application/json",
            **self._config.extra_headers,
        }
        self._http = httpx.AsyncClient(
            base_url=self._config.gateway_url,
            headers=headers,
            timeout=self._config.timeout,
        )
        logger.debug(
            "OpenClawBridge connected to %s (model=%s)",
            self._config.gateway_url,
            self._config.model,
        )

    async def disconnect(self) -> None:
        """Close the underlying HTTP client."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None
            logger.debug("OpenClawBridge disconnected")

    async def __aenter__(self) -> "OpenClawBridge":
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> httpx.AsyncClient:
        if self._http is None:
            msg = (
                "OpenClawBridge is not connected. "
                "Call connect() or use as an async context manager."
            )
            raise RuntimeError(msg)
        return self._http

    @staticmethod
    def _extract_text(message: A2AMessage) -> str:
        """Return the concatenated text from all TextParts in *message*."""
        parts = [p.text for p in message.parts if isinstance(p, TextPart)]
        return "\n".join(parts)

    @staticmethod
    def _make_task_id() -> str:
        return f"task-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _make_context_id() -> str:
        return f"ctx-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _make_artifact_id() -> str:
        return f"artifact-{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------------
    # Core translation methods
    # ------------------------------------------------------------------

    def _build_completions_payload(
        self,
        text: str,
        *,
        system_prompt: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Build an OpenAI-compatible chat completions payload for OpenClaw.

        Parameters
        ----------
        text:
            The user message text.
        system_prompt:
            Optional system prompt prepended as a ``"system"`` message.
        history:
            Optional list of prior ``{"role": ..., "content": ...}`` turns
            to include for multi-turn context.

        """
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": text})

        return {
            "model": self._config.model,
            "messages": messages,
            "stream": False,
        }

    def _parse_completions_response(self, body: dict[str, Any]) -> str:
        """Extract the assistant reply text from an OpenAI-style response body."""
        raw_choices = body.get("choices")
        choices: list[dict[str, Any]] = (
            cast(list[dict[str, Any]], raw_choices) if isinstance(raw_choices, list) else []
        )
        if not choices:
            logger.warning("OpenClaw returned empty choices list")
            return ""
        first: dict[str, Any] = choices[0]
        raw_message = first.get("message")
        message: dict[str, Any] = cast(dict[str, Any], raw_message) if raw_message else {}
        content = message.get("content")
        return str(content) if content is not None else ""

    def _build_completed_task(
        self,
        reply_text: str,
        *,
        task_id: str,
        context_id: str,
        input_message: A2AMessage,
    ) -> A2ATask:
        """Wrap *reply_text* in a completed :class:`~obscura.core.models.a2a.A2ATask`."""
        now = datetime.now(UTC)

        artifact = Artifact(
            artifactId=self._make_artifact_id(),
            name="response",
            parts=[TextPart(text=reply_text)],
        )

        reply_message = A2AMessage(
            role=A2ARole.AGENT,
            messageId=f"msg-{uuid.uuid4().hex[:8]}",
            parts=[TextPart(text=reply_text)],
            taskId=task_id,
            contextId=context_id,
            timestamp=now,
        )

        status = A2ATaskStatus(
            state=A2ATaskState.COMPLETED,
            message=reply_message,
            timestamp=now,
        )

        return A2ATask(
            id=task_id,
            contextId=context_id,
            status=status,
            artifacts=[artifact],
            history=[input_message, reply_message],
        )

    def _build_failed_task(
        self,
        error_msg: str,
        *,
        task_id: str,
        context_id: str,
    ) -> A2ATask:
        """Return a ``failed`` :class:`~obscura.core.models.a2a.A2ATask` for *error_msg*."""
        now = datetime.now(UTC)

        error_part = TextPart(text=error_msg)
        error_message = A2AMessage(
            role=A2ARole.AGENT,
            messageId=f"msg-{uuid.uuid4().hex[:8]}",
            parts=[error_part],
            taskId=task_id,
            contextId=context_id,
            timestamp=now,
        )

        return A2ATask(
            id=task_id,
            contextId=context_id,
            status=A2ATaskStatus(
                state=A2ATaskState.FAILED,
                message=error_message,
                timestamp=now,
            ),
            artifacts=[],
            history=[error_message],
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send(
        self,
        text: str,
        *,
        task_id: str | None = None,
        context_id: str | None = None,
        system_prompt: str | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> A2ATask:
        """Send a text message to OpenClaw and return a completed A2A task.

        Parameters
        ----------
        text:
            User message to send.
        task_id:
            Explicit task ID.  A UUID-derived value is generated when omitted.
        context_id:
            Explicit context ID.  A UUID-derived value is generated when omitted.
        system_prompt:
            Optional system message prepended to the completions request.
        history:
            Prior conversation turns in OpenAI ``{"role", "content"}`` format.

        Returns
        -------
        A2ATask
            A task in ``completed`` or ``failed`` state.

        """
        http = self._ensure_connected()
        tid = task_id or self._make_task_id()
        cid = context_id or self._make_context_id()

        now = datetime.now(UTC)
        input_message = A2AMessage(
            role=A2ARole.USER,
            messageId=f"msg-{uuid.uuid4().hex[:8]}",
            parts=[TextPart(text=text)],
            taskId=tid,
            contextId=cid,
            timestamp=now,
        )

        payload = self._build_completions_payload(
            text,
            system_prompt=system_prompt,
            history=history,
        )

        try:
            resp = await http.post("/v1/chat/completions", json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            error_msg = (
                f"OpenClaw returned HTTP {exc.response.status_code}: "
                f"{exc.response.text[:200]}"
            )
            logger.error("OpenClaw HTTP error: %s", error_msg)
            return self._build_failed_task(error_msg, task_id=tid, context_id=cid)
        except httpx.TransportError as exc:
            error_msg = f"OpenClaw transport error: {exc}"
            logger.error("OpenClaw connection error: %s", error_msg)
            return self._build_failed_task(error_msg, task_id=tid, context_id=cid)

        body: dict[str, Any] = resp.json()
        reply_text = self._parse_completions_response(body)

        logger.debug(
            "OpenClaw response: task=%s len=%d chars",
            tid,
            len(reply_text),
        )
        return self._build_completed_task(
            reply_text,
            task_id=tid,
            context_id=cid,
            input_message=input_message,
        )

    async def send_a2a_message(self, message: A2AMessage) -> A2ATask:
        """Translate an :class:`~obscura.core.models.a2a.A2AMessage` to OpenClaw.

        Convenience wrapper over :meth:`send` for callers that already have an
        ``A2AMessage`` object (e.g. an A2A service handler forwarding inbound
        ``message/send`` requests to OpenClaw).

        Parameters
        ----------
        message:
            Inbound A2A message.  Text is extracted from all
            :class:`~obscura.core.models.a2a.TextPart` parts.

        """
        text = self._extract_text(message)
        return await self.send(
            text,
            task_id=message.taskId,
            context_id=message.contextId,
        )

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream_send(
        self,
        text: str,
        history: list[dict[str, str]] | None = None,
        *,
        task_id: str | None = None,
        context_id: str | None = None,
        system_prompt: str | None = None,
    ) -> AsyncGenerator[A2AStatusUpdateEvent]:
        """Send *text* to OpenClaw and yield :class:`~obscura.core.models.a2a.A2AStatusUpdateEvent` chunks.

        The generator yields one event per SSE chunk while the model streams,
        then a final event with ``final=True`` and ``state=completed`` when
        the stream ends.  If OpenClaw does not support streaming (or any error
        occurs), a single completed event is yielded as a graceful fallback.

        Parameters
        ----------
        text:
            User message to send.
        history:
            Prior conversation turns in OpenAI ``{"role", "content"}`` format.
        task_id:
            Explicit task ID.  A UUID-derived value is generated when omitted.
        context_id:
            Explicit context ID.  A UUID-derived value is generated when omitted.
        system_prompt:
            Optional system message prepended to the completions request.

        Yields
        ------
        A2AStatusUpdateEvent
            Intermediate events with ``state=working`` while streaming;
            a final event with ``state=completed`` (``final=True``) at the end.

        """
        http = self._ensure_connected()
        tid = task_id or self._make_task_id()
        cid = context_id or self._make_context_id()

        payload = self._build_completions_payload(
            text,
            system_prompt=system_prompt,
            history=history,
        )
        payload["stream"] = True

        accumulated: list[str] = []

        def _working_event(chunk_text: str) -> A2AStatusUpdateEvent:
            now = datetime.now(UTC)
            chunk_msg = A2AMessage(
                role=A2ARole.AGENT,
                messageId=f"msg-{uuid.uuid4().hex[:8]}",
                parts=[TextPart(text=chunk_text)],
                taskId=tid,
                contextId=cid,
                timestamp=now,
            )
            return A2AStatusUpdateEvent(
                taskId=tid,
                contextId=cid,
                status=A2ATaskStatus(
                    state=A2ATaskState.WORKING,
                    message=chunk_msg,
                    timestamp=now,
                ),
                final=False,
            )

        def _completed_event(reply_text: str) -> A2AStatusUpdateEvent:
            now = datetime.now(UTC)
            reply_msg = A2AMessage(
                role=A2ARole.AGENT,
                messageId=f"msg-{uuid.uuid4().hex[:8]}",
                parts=[TextPart(text=reply_text)],
                taskId=tid,
                contextId=cid,
                timestamp=now,
            )
            return A2AStatusUpdateEvent(
                taskId=tid,
                contextId=cid,
                status=A2ATaskStatus(
                    state=A2ATaskState.COMPLETED,
                    message=reply_msg,
                    timestamp=now,
                ),
                final=True,
            )

        try:
            async with http.stream("POST", "/v1/chat/completions", json=payload) as resp:
                resp.raise_for_status()
                async for raw_line in resp.aiter_lines():
                    line = raw_line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk: dict[str, Any] = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    raw_choices = chunk.get("choices")
                    choices: list[dict[str, Any]] = (
                        cast(list[dict[str, Any]], raw_choices)
                        if isinstance(raw_choices, list)
                        else []
                    )
                    if not choices:
                        continue

                    delta = cast(dict[str, Any], choices[0].get("delta") or {})
                    content = delta.get("content")
                    if content:
                        token = str(content)
                        accumulated.append(token)
                        yield _working_event(token)

        except Exception as exc:
            logger.debug(
                "OpenClaw stream_send fell back to non-streaming (task=%s): %s",
                tid,
                exc,
            )
            # Graceful fallback: call blocking send and emit a single event
            task = await self.send(
                text,
                task_id=tid,
                context_id=cid,
                system_prompt=system_prompt,
                history=history,
            )
            reply_text = ""
            if task.artifacts:
                artifact_parts = task.artifacts[0].parts
                reply_text = "".join(
                    p.text for p in artifact_parts if isinstance(p, TextPart)
                )
            yield _completed_event(reply_text)
            return

        yield _completed_event("".join(accumulated))

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Return ``True`` if the OpenClaw gateway is reachable.

        Sends a minimal single-token completions request as a liveness probe.
        Safe to call repeatedly — does not affect conversation state.
        """
        http = self._ensure_connected()
        try:
            resp = await http.post(
                "/v1/chat/completions",
                json={
                    "model": self._config.model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                    "stream": False,
                },
                timeout=5.0,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.debug("OpenClaw health check failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Multi-turn context
# ---------------------------------------------------------------------------


class OpenClawContext:
    """Manages multi-turn conversation state for :class:`OpenClawBridge`.

    Maintains a running OpenAI-format message history so each call to
    :meth:`send` automatically includes prior turns.  Useful for back-and-forth
    conversations where the caller does not want to manage history manually.

    Parameters
    ----------
    context_id:
        Identifier for this context window.  A UUID is generated when omitted.

    Examples
    --------
    ::

        async with bridge:
            ctx = OpenClawContext()
            t1 = await ctx.send(bridge, "Hello!")
            t2 = await ctx.send(bridge, "What did I just say?")
            print(len(ctx))   # 2

    """

    def __init__(self, context_id: str | None = None) -> None:
        self.context_id: str = context_id or f"ctx-{uuid.uuid4().hex[:12]}"
        self.history: list[dict[str, str]] = []

    async def send(self, bridge: OpenClawBridge, text: str) -> A2ATask:
        """Append *text* as a user turn, call *bridge*, record the reply.

        Parameters
        ----------
        bridge:
            A connected :class:`OpenClawBridge` instance.
        text:
            User message for this turn.

        Returns
        -------
        A2ATask
            The completed (or failed) task returned by the bridge.

        """
        self.history.append({"role": "user", "content": text})
        task = await bridge.send(
            text,
            context_id=self.context_id,
            history=self.history[:-1],  # send prior turns; bridge appends current
        )
        # Extract reply text from the task artifact for history
        reply_text = ""
        if task.artifacts:
            reply_text = "".join(
                p.text for p in task.artifacts[0].parts if isinstance(p, TextPart)
            )
        self.history.append({"role": "assistant", "content": reply_text})
        return task

    def clear(self) -> None:
        """Reset conversation history (keeps context_id)."""
        self.history = []

    def __len__(self) -> int:
        """Return the number of completed turns (user+assistant pairs)."""
        return len(self.history) // 2


__all__ = [
    "OpenClawBridge",
    "OpenClawBridgeConfig",
    "OpenClawContext",
]
