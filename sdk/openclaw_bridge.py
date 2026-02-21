"""
sdk.openclaw_bridge -- Async OpenClaw-facing bridge for Obscura HTTP API.

This module provides a small, typed client surface that OpenClaw can use to
spawn/run agents, interact with key-value memory, and perform semantic search.
"""

from __future__ import annotations

import uuid
import asyncio
from dataclasses import dataclass, field
from typing import Any, cast

import httpx


@dataclass(frozen=True)
class OpenClawBridgeConfig:
    """Configuration for OpenClawBridge."""

    base_url: str = "http://localhost:8080"
    token: str = "local-dev-token"
    timeout_seconds: float = 300.0
    workflow_max_retries: int = 2
    workflow_retry_backoff_seconds: float = 0.25


@dataclass(frozen=True)
class RequestMetadata:
    """Per-request metadata for tracing and idempotency."""

    correlation_id: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class BackendRoutingPolicy:
    """Declarative task-type to backend-model routing policy."""

    default_model: str = "claude"
    routes: dict[str, str] = field(
        default_factory=lambda: {
            "review": "claude",
            "analysis": "claude",
            "summarize": "copilot",
            "codegen": "openai",
            "testgen": "openai",
            "support": "copilot",
        }
    )
    fallback_order: tuple[str, ...] = ("claude", "copilot", "openai", "localllm")
    fallback_routes: dict[str, tuple[str, ...]] = field(default_factory=lambda: {})

    def select_model(self, task_type: str) -> str:
        normalized = task_type.strip().lower()
        if not normalized:
            return self.default_model
        return self.routes.get(normalized, self.default_model)

    def model_candidates(self, task_type: str, explicit_model: str | None) -> list[str]:
        normalized = task_type.strip().lower()
        primary = explicit_model or self.select_model(task_type)
        candidates: list[str] = [primary]
        for model in self.fallback_routes.get(normalized, ()):
            if model not in candidates:
                candidates.append(model)
        for model in self.fallback_order:
            if model not in candidates:
                candidates.append(model)
        return candidates


@dataclass(frozen=True)
class SpawnAgentRequest:
    """Request payload for POST /api/v1/agents."""

    name: str
    model: str = "claude"
    system_prompt: str = ""
    memory_namespace: str = "openclaw"
    max_iterations: int | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "model": self.model,
            "system_prompt": self.system_prompt,
            "memory_namespace": self.memory_namespace,
        }
        if self.max_iterations is not None:
            payload["max_iterations"] = self.max_iterations
        return payload


@dataclass(frozen=True)
class RunAgentRequest:
    """Request payload for POST /api/v1/agents/{id}/run."""

    prompt: str
    context: dict[str, Any] = field(default_factory=lambda: {})

    def to_payload(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "context": self.context,
        }


@dataclass(frozen=True)
class MemoryWriteRequest:
    """Request payload for POST /api/v1/memory/{namespace}/{key}."""

    namespace: str
    key: str
    value: Any


@dataclass(frozen=True)
class SemanticSearchRequest:
    """Request payload for GET /api/v1/vector-memory/search."""

    query: str
    top_k: int = 3
    namespace: str | None = None

    def to_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"q": self.query, "top_k": self.top_k}
        if self.namespace:
            params["namespace"] = self.namespace
        return params


@dataclass(frozen=True)
class WorkflowRunRequest:
    """High-level request for /api/v1/workflows/run."""

    task_type: str
    goal: str
    context: dict[str, Any] = field(default_factory=lambda: {})
    constraints: list[str] = field(default_factory=lambda: [])
    expected_output: str = ""
    model: str | None = None
    name: str = "openclaw-workflow"
    system_prompt: str = ""
    memory_namespace: str = "openclaw"
    store_result: bool = True
    memory_key: str = "last_result"

    def to_payload(self, model: str) -> dict[str, Any]:
        return {
            "name": self.name,
            "task_type": self.task_type,
            "goal": self.goal,
            "context": self.context,
            "constraints": self.constraints,
            "expected_output": self.expected_output,
            "model": model,
            "system_prompt": self.system_prompt,
            "memory_namespace": self.memory_namespace,
            "store_result": self.store_result,
            "memory_key": self.memory_key,
        }


@dataclass
class WorkflowAttemptTelemetry:
    """One workflow execution attempt for observability/debugging."""

    attempt: int
    model: str
    retry_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "model": self.model,
            "retry_reason": self.retry_reason,
        }


class OpenClawBridge:
    """Typed async bridge for OpenClaw integration against Obscura API."""

    def __init__(
        self,
        config: OpenClawBridgeConfig | None = None,
        *,
        routing_policy: BackendRoutingPolicy | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config or OpenClawBridgeConfig()
        self._routing_policy = routing_policy or BackendRoutingPolicy()
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> OpenClawBridge:
        self._ensure_client()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    @property
    def config(self) -> OpenClawBridgeConfig:
        return self._config

    @property
    def routing_policy(self) -> BackendRoutingPolicy:
        return self._routing_policy

    async def aclose(self) -> None:
        """Close the underlying HTTP client if owned by this bridge."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def spawn_agent(
        self,
        request: SpawnAgentRequest,
        metadata: RequestMetadata | None = None,
    ) -> dict[str, Any]:
        """Spawn a new agent."""
        resp = await self._http().post(
            "/api/v1/agents",
            json=request.to_payload(),
            headers=self._headers(metadata),
        )
        resp.raise_for_status()
        return resp.json()

    async def run_agent(
        self,
        agent_id: str,
        request: RunAgentRequest,
        metadata: RequestMetadata | None = None,
    ) -> dict[str, Any]:
        """Run work on an existing agent."""
        resp = await self._http().post(
            f"/api/v1/agents/{agent_id}/run",
            json=request.to_payload(),
            headers=self._headers(metadata),
        )
        resp.raise_for_status()
        return resp.json()

    async def get_agent_status(
        self,
        agent_id: str,
        metadata: RequestMetadata | None = None,
    ) -> dict[str, Any]:
        """Fetch status/details for one agent."""
        resp = await self._http().get(
            f"/api/v1/agents/{agent_id}",
            headers=self._headers(metadata),
        )
        resp.raise_for_status()
        return resp.json()

    async def store_memory(
        self,
        request: MemoryWriteRequest,
        metadata: RequestMetadata | None = None,
    ) -> None:
        """Store a key-value memory entry."""
        resp = await self._http().post(
            f"/api/v1/memory/{request.namespace}/{request.key}",
            json={"value": request.value},
            headers=self._headers(metadata),
        )
        resp.raise_for_status()

    async def get_memory(
        self,
        namespace: str,
        key: str,
        metadata: RequestMetadata | None = None,
    ) -> Any | None:
        """Read a key-value memory entry, returning None if absent."""
        resp = await self._http().get(
            f"/api/v1/memory/{namespace}/{key}",
            headers=self._headers(metadata),
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("value")

    async def semantic_search(
        self,
        request: SemanticSearchRequest,
        metadata: RequestMetadata | None = None,
    ) -> list[dict[str, Any]]:
        """Run semantic vector-memory search."""
        resp = await self._http().get(
            "/api/v1/vector-memory/search",
            params=request.to_params(),
            headers=self._headers(metadata),
        )
        resp.raise_for_status()
        body = resp.json()
        return body.get("results", [])

    async def run_workflow(
        self,
        request: WorkflowRunRequest,
        metadata: RequestMetadata | None = None,
    ) -> dict[str, Any]:
        """Run high-level workflow task with retries and model fallbacks."""
        candidates = self._routing_policy.model_candidates(
            request.task_type, request.model
        )
        errors: list[str] = []
        attempts: list[WorkflowAttemptTelemetry] = []

        for model in candidates:
            for attempt in range(self._config.workflow_max_retries + 1):
                current_attempt = WorkflowAttemptTelemetry(
                    attempt=attempt + 1,
                    model=model,
                    retry_reason="",
                )
                attempts.append(current_attempt)
                try:
                    resp = await self._http().post(
                        "/api/v1/workflows/run",
                        json=request.to_payload(model),
                        headers=self._headers(metadata),
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    if isinstance(body, dict):
                        body_map = cast(dict[str, Any], body)
                        result = dict(body_map)
                        result["telemetry"] = {
                            "attempts": [a.to_dict() for a in attempts]
                        }
                        return result
                    return {
                        "result": body,
                        "telemetry": {"attempts": [a.to_dict() for a in attempts]},
                    }
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if self._is_retryable_status(status) and attempt < self._config.workflow_max_retries:
                        current_attempt.retry_reason = f"http_{status}"
                        await self._sleep_backoff(attempt)
                        continue
                    current_attempt.retry_reason = f"http_{status}"
                    errors.append(f"model={model} status={status}")
                    # 4xx validation/auth errors are not retriable and should stop fallback.
                    if 400 <= status < 500 and status not in (401, 402, 403, 408, 429):
                        raise
                    break
                except httpx.RequestError as exc:
                    if attempt < self._config.workflow_max_retries:
                        current_attempt.retry_reason = type(exc).__name__
                        await self._sleep_backoff(attempt)
                        continue
                    current_attempt.retry_reason = type(exc).__name__
                    errors.append(f"model={model} request_error={type(exc).__name__}")
                    break

        raise RuntimeError(
            "Workflow failed across fallback chain: "
            + "; ".join(errors)
            + f"; attempts={[a.to_dict() for a in attempts]}"
        )

    async def health(self, metadata: RequestMetadata | None = None) -> dict[str, Any]:
        """Check server health."""
        resp = await self._http().get("/health", headers=self._headers(metadata))
        resp.raise_for_status()
        return resp.json()

    def _ensure_client(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._config.base_url,
                headers={"Authorization": f"Bearer {self._config.token}"},
                timeout=self._config.timeout_seconds,
            )

    def _http(self) -> httpx.AsyncClient:
        self._ensure_client()
        if self._client is None:
            raise RuntimeError("OpenClawBridge HTTP client is not initialized.")
        return self._client

    @staticmethod
    def _headers(metadata: RequestMetadata | None) -> dict[str, str]:
        request_id = metadata.correlation_id if metadata else None
        if not request_id:
            request_id = str(uuid.uuid4())
        headers = {"x-request-id": request_id}
        if metadata and metadata.idempotency_key:
            headers["x-idempotency-key"] = metadata.idempotency_key
        return headers

    async def _sleep_backoff(self, attempt: int) -> None:
        delay = self._config.workflow_retry_backoff_seconds * (2 ** attempt)
        if delay > 0:
            await asyncio.sleep(delay)

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code in (408, 425, 429, 500, 502, 503, 504)
