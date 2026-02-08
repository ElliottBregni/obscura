"""
End-to-End Tests for Obscura

These tests verify complete workflows from API to response.
Uses FastAPI TestClient with auth disabled — no running server needed.

Usage:
    pytest tests/e2e/ -v
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, auto
from typing import Any, AsyncIterator
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from sdk.auth.models import AuthenticatedUser
from sdk.auth.rbac import get_current_user
from sdk.config import ObscuraConfig
from sdk.server import create_app


# ---------------------------------------------------------------------------
# Test user returned when auth is disabled
# ---------------------------------------------------------------------------

_TEST_USER = AuthenticatedUser(
    user_id="test-user",
    email="test@obscura.dev",
    roles=("admin", "agent:copilot", "agent:claude", "agent:read", "sync:write", "sessions:manage"),
    org_id="test-org",
    token_type="user",
    raw_token="test-token",
)


# ---------------------------------------------------------------------------
# Fake agent objects for mocking AgentRuntime
# ---------------------------------------------------------------------------

class _FakeAgentStatus(Enum):
    PENDING = auto()
    RUNNING = auto()
    STOPPED = auto()


@dataclass
class _FakeAgentConfig:
    name: str
    model: str = "claude"
    system_prompt: str = ""
    memory_namespace: str = "default"
    max_iterations: int = 10
    tags: list = field(default_factory=list)


@dataclass
class _FakeAgentState:
    agent_id: str
    name: str
    status: _FakeAgentStatus
    created_at: datetime
    updated_at: datetime
    iteration_count: int = 0
    error_message: str | None = None


class _FakeAgent:
    """Minimal fake Agent that satisfies the server endpoints."""

    def __init__(self, name: str = "test-agent", model: str = "claude", **kwargs: Any) -> None:
        self.id = str(uuid.uuid4())
        self.config = _FakeAgentConfig(name=name, model=model, **kwargs)
        self.status = _FakeAgentStatus.RUNNING
        self.created_at = datetime.now(UTC)

    async def start(self) -> None:
        pass

    async def run(self, prompt: str, **context: Any) -> str:
        return f"echo: {prompt}"

    async def stream(self, prompt: str, **context: Any) -> AsyncIterator[str]:
        yield f"echo: {prompt}"

    async def stop(self) -> None:
        self.status = _FakeAgentStatus.STOPPED

    async def send_message(self, target: str, content: str) -> None:
        """Send a message to another agent."""
        pass

    def get_state(self) -> _FakeAgentState:
        return _FakeAgentState(
            agent_id=self.id,
            name=self.config.name,
            status=self.status,
            created_at=self.created_at,
            updated_at=datetime.now(UTC),
        )


class _FakeAgentRuntime:
    """In-memory fake AgentRuntime."""

    def __init__(self, user: Any = None) -> None:
        self._agents: dict[str, _FakeAgent] = {}

    async def start(self) -> None:
        pass

    def spawn(self, name: str = "unnamed", model: str = "claude", **kwargs: Any) -> _FakeAgent:
        agent = _FakeAgent(name=name, model=model, **kwargs)
        self._agents[agent.id] = agent
        return agent

    def get_agent(self, agent_id: str) -> _FakeAgent | None:
        return self._agents.get(agent_id)

    def get_agent_status(self, agent_id: str) -> _FakeAgentState | None:
        agent = self._agents.get(agent_id)
        if agent is None:
            return None
        return agent.get_state()

    def list_agents(self, status: Any = None, name: str | None = None) -> list[_FakeAgent]:
        return list(self._agents.values())


# ---------------------------------------------------------------------------
# Fake memory objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _FakeMemoryKey:
    namespace: str
    key: str


class _FakeMemoryStore:
    """In-memory fake MemoryStore."""

    _stores: dict[str, "_FakeMemoryStore"] = {}

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    @classmethod
    def for_user(cls, user: Any) -> "_FakeMemoryStore":
        uid = getattr(user, "user_id", "test")
        if uid not in cls._stores:
            cls._stores[uid] = cls()
        return cls._stores[uid]

    @classmethod
    def reset(cls) -> None:
        cls._stores.clear()

    def set(self, key: str, value: Any, namespace: str = "default", ttl: Any = None) -> None:
        self._data[f"{namespace}:{key}"] = value

    def get(self, key: str, namespace: str = "default", default: Any = None) -> Any:
        return self._data.get(f"{namespace}:{key}", default)

    def delete(self, key: str, namespace: str = "default") -> bool:
        k = f"{namespace}:{key}"
        if k in self._data:
            del self._data[k]
            return True
        return False

    def list_keys(self, namespace: str | None = None) -> list[_FakeMemoryKey]:
        keys = []
        for k in self._data:
            ns, name = k.split(":", 1)
            if namespace is None or ns == namespace:
                keys.append(_FakeMemoryKey(namespace=ns, key=name))
        return keys

    def search(self, query: str) -> list[tuple[_FakeMemoryKey, Any]]:
        results = []
        for k, v in self._data.items():
            ns, name = k.split(":", 1)
            if query.lower() in str(v).lower() or query.lower() in k.lower():
                results.append((_FakeMemoryKey(namespace=ns, key=name), v))
        return results

    def get_stats(self) -> dict[str, Any]:
        return {"total_keys": len(self._data)}


@dataclass
class _FakeVectorResult:
    key: _FakeMemoryKey
    text: str
    score: float
    metadata: dict[str, Any]


class _FakeVectorMemoryStore:
    """In-memory fake VectorMemoryStore."""

    _stores: dict[str, "_FakeVectorMemoryStore"] = {}

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}

    @classmethod
    def for_user(cls, user: Any, **kwargs: Any) -> "_FakeVectorMemoryStore":
        uid = getattr(user, "user_id", "test")
        if uid not in cls._stores:
            cls._stores[uid] = cls()
        return cls._stores[uid]

    @classmethod
    def reset(cls) -> None:
        cls._stores.clear()

    def set(self, key: str, text: str, metadata: dict | None = None, namespace: str = "default", **kw: Any) -> None:
        self._data[f"{namespace}:{key}"] = {"text": text, "metadata": metadata or {}}

    def search_similar(self, query: str, namespace: str | None = None, top_k: int = 5, **kw: Any) -> list[_FakeVectorResult]:
        results = []
        for k, v in self._data.items():
            ns, name = k.split(":", 1)
            if namespace is None or ns == namespace:
                results.append(_FakeVectorResult(
                    key=_FakeMemoryKey(namespace=ns, key=name),
                    text=v["text"],
                    score=0.9,
                    metadata=v["metadata"],
                ))
        return results[:top_k]

    def delete(self, key: str, namespace: str = "default") -> bool:
        k = f"{namespace}:{key}"
        if k in self._data:
            del self._data[k]
            return True
        return False


# ---------------------------------------------------------------------------
# Shared runtime instance (per-test reset)
# ---------------------------------------------------------------------------

_shared_runtime = _FakeAgentRuntime()


async def _mock_get_runtime(user: Any) -> _FakeAgentRuntime:
    return _shared_runtime


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_fakes():
    """Reset all fake stores between tests."""
    global _shared_runtime
    _shared_runtime = _FakeAgentRuntime()
    _FakeMemoryStore.reset()
    _FakeVectorMemoryStore.reset()
    yield


@pytest.fixture
def client():
    """TestClient with auth disabled and mocked backends."""
    config = ObscuraConfig(
        auth_enabled=False,
        otel_enabled=False,
    )

    with (
        patch("sdk.server._get_runtime", side_effect=_mock_get_runtime),
        patch("sdk.memory.MemoryStore", _FakeMemoryStore),
        patch("sdk.vector_memory.VectorMemoryStore", _FakeVectorMemoryStore),
    ):
        app = create_app(config)
        # Bypass RBAC: return a test user with all roles
        app.dependency_overrides[get_current_user] = lambda: _TEST_USER
        with TestClient(app) as tc:
            yield tc


@pytest.fixture
def client_no_auth_override():
    """TestClient with auth ENABLED but no dependency override.
    
    Use this to test actual auth behavior (API keys, JWT).
    """
    config = ObscuraConfig(
        auth_enabled=True,
        otel_enabled=False,
    )

    with (
        patch("sdk.server._get_runtime", side_effect=_mock_get_runtime),
        patch("sdk.memory.MemoryStore", _FakeMemoryStore),
        patch("sdk.vector_memory.VectorMemoryStore", _FakeVectorMemoryStore),
    ):
        app = create_app(config)
        # NO dependency override - tests real auth flow
        with TestClient(app) as tc:
            yield tc
