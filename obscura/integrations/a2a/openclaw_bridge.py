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
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from collections.abc import AsyncGenerator

import httpx

from obscura.core.circuit_breaker import CircuitBreaker, CircuitOpenError
from obscura.core.config import ObscuraConfig
from obscura.core.enums.protocol import A2ARole, A2ATaskState
from obscura.core.models.a2a import (
    A2AMessage,
    A2AStatusUpdateEvent,
    A2ATask,
    A2ATaskStatus,
    Artifact,
    TextPart,
)
from obscura.core.retry import with_retry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_GATEWAY_URL = "http://localhost:18789"
_DEFAULT_MODEL = "openclaw/main"
_DEFAULT_TIMEOUT = 120.0
_MAX_TEXT_LEN = 32_000

# Matches control characters except horizontal tab (\x09) and newline (\x0a)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_VALID_ROLES = {"user", "assistant", "system"}

_AUDIT_LOG_PATH = Path.home() / ".obscura" / "logs" / "a2a-bridge.jsonl"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _cfg_max_retries() -> int:
    """Return max_retries from OscuraConfig (env/settings aware)."""
    return ObscuraConfig.load().max_retries


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
    max_retries:
        Maximum number of retries on 5xx or network errors.  Defaults to
        ``OscuraConfig.load().max_retries`` (env ``OBSCURA_MAX_RETRIES``).

    """

    token: str
    gateway_url: str = _DEFAULT_GATEWAY_URL
    model: str = _DEFAULT_MODEL
    timeout: float = _DEFAULT_TIMEOUT
    extra_headers: dict[str, str] = field(default_factory=dict[str, str])
    max_retries: int = field(default_factory=_cfg_max_retries)


# ---------------------------------------------------------------------------
# Input sanitization helpers
# ---------------------------------------------------------------------------


def _sanitize_text(text: str) -> str:
    """Strip null bytes and control chars (except \\t and \\n); truncate to configured limit.

    The truncation limit is read from ``OscuraConfig.load().a2a_bridge_max_text_len``
    (env ``OBSCURA_A2A_BRIDGE_MAX_TEXT_LEN``, default 32 000).
    """
    max_len = ObscuraConfig.load().a2a_bridge_max_text_len
    cleaned = _CTRL_RE.sub("", text)
    if len(cleaned) > max_len:
        logger.warning(
            "OpenClawBridge: input text truncated from %d to %d chars",
            len(cleaned),
            max_len,
        )
        cleaned = cleaned[:max_len]
    return cleaned


def _sanitize_history(history: list[dict[str, str]] | None) -> list[dict[str, str]]:
    """Return a cleaned copy of *history*, dropping entries with invalid role/content."""
    if not history:
        return []
    out: list[dict[str, str]] = []
    for entry in history:
        role = entry.get("role")
        content = entry.get("content")
        if role not in _VALID_ROLES:
            logger.warning(
                "OpenClawBridge: dropping history entry with invalid role %r", role
            )
            continue
        if not isinstance(content, str):
            logger.warning(
                "OpenClawBridge: dropping history entry with non-string content (role=%r)",
                role,
            )
            continue
        out.append({"role": role, "content": content})
    return out


# ---------------------------------------------------------------------------
# Retry predicate
# ---------------------------------------------------------------------------


def _is_retryable(exc: Exception) -> bool:
    """Return True for 5xx HTTP errors and network-level transport errors."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


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
        _cfg = ObscuraConfig.load()
        self._circuit_breaker = CircuitBreaker(
            name="openclaw",
            failure_threshold=_cfg.circuit_breaker_threshold,
            recovery_timeout=_cfg.circuit_breaker_recovery,
        )

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

    def _audit_log(
        self,
        *,
        text_len: int,
        state: str,
        duration_ms: float,
    ) -> None:
        """Append a JSON audit line to ~/.obscura/logs/a2a-bridge.jsonl."""
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "direction": "out",
            "model": self._config.model,
            "text_len": text_len,
            "state": state,
            "duration_ms": round(duration_ms, 2),
        }
        try:
            _AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _AUDIT_LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            logger.debug("OpenClawBridge: failed to write audit log", exc_info=True)

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
            Entries with invalid ``role`` or non-string ``content`` are dropped.

        Returns
        -------
        A2ATask
            A task in ``completed`` or ``failed`` state.

        """
        http = self._ensure_connected()
        tid = task_id or self._make_task_id()
        cid = context_id or self._make_context_id()

        # --- input sanitization ---
        clean_text = _sanitize_text(text)
        clean_history = _sanitize_history(history)

        now = datetime.now(UTC)
        input_message = A2AMessage(
            role=A2ARole.USER,
            messageId=f"msg-{uuid.uuid4().hex[:8]}",
            parts=[TextPart(text=clean_text)],
            taskId=tid,
            contextId=cid,
            timestamp=now,
        )

        payload = self._build_completions_payload(
            clean_text,
            system_prompt=system_prompt,
            history=clean_history or None,
        )

        # --- circuit breaker: fast-fail if open ---
        if not self._circuit_breaker.allow_request():
            retry_after = self._circuit_breaker.time_until_half_open()
            error_msg = (
                f"OpenClaw circuit breaker is open; retry after {retry_after:.1f}s"
            )
            logger.warning("OpenClawBridge: %s", error_msg)
            self._audit_log(
                text_len=len(clean_text),
                state="circuit_open",
                duration_ms=0.0,
            )
            return self._build_failed_task(error_msg, task_id=tid, context_id=cid)

        t_start = time.monotonic()
        final_state = "failed"

        async def _do_post() -> httpx.Response:
            resp = await http.post("/v1/chat/completions", json=payload)
            resp.raise_for_status()
            return resp

        try:
            resp = await with_retry(
                _do_post,
                max_retries=self._config.max_retries,
                initial_backoff=1.0,
                max_backoff=4.0,
                jitter=False,
                circuit=self._circuit_breaker,
                retryable=_is_retryable,
            )
        except CircuitOpenError as exc:
            error_msg = str(exc)
            logger.warning("OpenClawBridge: %s", error_msg)
            duration_ms = (time.monotonic() - t_start) * 1000
            self._audit_log(
                text_len=len(clean_text),
                state="circuit_open",
                duration_ms=duration_ms,
            )
            return self._build_failed_task(error_msg, task_id=tid, context_id=cid)
        except httpx.HTTPStatusError as exc:
            error_msg = (
                f"OpenClaw returned HTTP {exc.response.status_code}: "
                f"{exc.response.text[:200]}"
            )
            logger.error("OpenClaw HTTP error: %s", error_msg)
            duration_ms = (time.monotonic() - t_start) * 1000
            self._audit_log(
                text_len=len(clean_text),
                state=final_state,
                duration_ms=duration_ms,
            )
            return self._build_failed_task(error_msg, task_id=tid, context_id=cid)
        except httpx.TransportError as exc:
            error_msg = f"OpenClaw transport error: {exc}"
            logger.error("OpenClaw connection error: %s", error_msg)
            duration_ms = (time.monotonic() - t_start) * 1000
            self._audit_log(
                text_len=len(clean_text),
                state=final_state,
                duration_ms=duration_ms,
            )
            return self._build_failed_task(error_msg, task_id=tid, context_id=cid)

        body: dict[str, Any] = resp.json()
        reply_text = self._parse_completions_response(body)
        final_state = "completed"

        logger.debug(
            "OpenClaw response: task=%s len=%d chars",
            tid,
            len(reply_text),
        )

        duration_ms = (time.monotonic() - t_start) * 1000
        self._audit_log(
            text_len=len(clean_text),
            state=final_state,
            duration_ms=duration_ms,
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
            :class:`~obscura.core.models.a2a.TextPart` parts and sanitized
            before sending.

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

        # --- input sanitization ---
        clean_text = _sanitize_text(text)
        clean_history = _sanitize_history(history)

        # --- circuit breaker: fast-fail if open ---
        if not self._circuit_breaker.allow_request():
            retry_after = self._circuit_breaker.time_until_half_open()
            error_msg = (
                f"OpenClaw circuit breaker is open; retry after {retry_after:.1f}s"
            )
            logger.warning("OpenClawBridge stream_send: %s", error_msg)
            now = datetime.now(UTC)
            err_message = A2AMessage(
                role=A2ARole.AGENT,
                messageId=f"msg-{uuid.uuid4().hex[:8]}",
                parts=[TextPart(text=error_msg)],
                taskId=tid,
                contextId=cid,
                timestamp=now,
            )
            yield A2AStatusUpdateEvent(
                taskId=tid,
                contextId=cid,
                status=A2ATaskStatus(
                    state=A2ATaskState.FAILED,
                    message=err_message,
                    timestamp=now,
                ),
                final=True,
            )
            return

        payload = self._build_completions_payload(
            clean_text,
            system_prompt=system_prompt,
            history=clean_history or None,
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
                self._circuit_breaker.record_success()
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
            self._circuit_breaker.record_failure()
            logger.debug(
                "OpenClaw stream_send fell back to non-streaming (task=%s): %s",
                tid,
                exc,
            )
            # Graceful fallback: call blocking send and emit a single event
            task = await self.send(
                clean_text,
                task_id=tid,
                context_id=cid,
                system_prompt=system_prompt,
                history=clean_history or None,
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
