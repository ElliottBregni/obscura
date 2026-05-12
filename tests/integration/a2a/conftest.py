"""Shared fixtures for A2A standalone-mode integration tests.

All tests run against an in-process FastAPI app using httpx ASGI
transport — no real network, no real LLM backend required.

``patch_session`` substitutes ``build_a2a_session`` with a fake that
returns a canned text response, so every task completes instantly.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI

from obscura.core.enums.protocol import A2ARole
from obscura.core.types import AgentEvent, AgentEventKind
from obscura.integrations.a2a.agent_card import AgentCardGenerator
from obscura.integrations.a2a.service import A2AService
from obscura.integrations.a2a.store import InMemoryTaskStore
from obscura.integrations.a2a.transports.jsonrpc import create_jsonrpc_router
from obscura.integrations.a2a.transports.rest import (
    create_rest_router,
    create_wellknown_router,
)
from obscura.integrations.a2a.transports.sse import create_sse_router
from obscura.integrations.a2a.types import A2AMessage, TextPart

# ---------------------------------------------------------------------------
# Shared constants (re-imported in test modules via ``from .conftest import …``)
# ---------------------------------------------------------------------------

TEST_AGENT_NAME = "integration-test-agent"
TEST_AGENT_URL = "http://testserver"
TEST_AGENT_DESC = "A2A standalone integration test agent"
FAKE_RESPONSE = "The test agent says hello."


# ---------------------------------------------------------------------------
# Fake agent session (zero LLM calls)
# ---------------------------------------------------------------------------


class _FakeSession:
    """Stub AgentSession — returns a canned text without calling any LLM."""

    def __init__(self, text: str = FAKE_RESPONSE) -> None:
        self.host_callbacks: dict[str, Any] = {}
        self._text = text

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def run_loop_to_text(self, prompt: str, **_: Any) -> str:  # noqa: ARG002
        return self._text

    async def stream_loop(
        self,
        prompt: str,
        **_: Any,  # noqa: ARG002
    ) -> AsyncIterator[AgentEvent]:
        """Yield a single TEXT_DELTA + AGENT_DONE so the SSE mapper fires."""

        async def _gen() -> AsyncIterator[AgentEvent]:
            yield AgentEvent(kind=AgentEventKind.TEXT_DELTA, text=self._text)
            yield AgentEvent(kind=AgentEventKind.AGENT_DONE, text=self._text)

        async for event in _gen():
            yield event


async def _fake_build_session(*_: Any, **__: Any) -> _FakeSession:
    """Drop-in replacement for ``obscura.composition.a2a.build_a2a_session``."""
    return _FakeSession()


# ---------------------------------------------------------------------------
# App / service factory helpers
# ---------------------------------------------------------------------------


def make_agent_card(url: str = TEST_AGENT_URL) -> Any:
    return (
        AgentCardGenerator(
            name=TEST_AGENT_NAME,
            url=url,
            description=TEST_AGENT_DESC,
        )
        .with_capabilities(streaming=True)
        .with_bearer_auth()
        .with_provider("Obscura", "https://obscura.dev")
        .build()
    )


def make_app(service: A2AService) -> FastAPI:
    app = FastAPI(title="A2A Integration Test Server")
    app.include_router(create_wellknown_router(service))
    app.include_router(create_rest_router(service))
    app.include_router(create_jsonrpc_router(service))
    app.include_router(create_sse_router(service))
    return app


def user_message(text: str = "ping") -> A2AMessage:
    return A2AMessage(
        role=A2ARole.USER,
        messageId=f"msg-{uuid.uuid4().hex[:8]}",
        parts=[TextPart(text=text)],
    )


def user_message_dict(text: str = "ping") -> dict[str, Any]:
    return {
        "role": "user",
        "messageId": f"msg-{uuid.uuid4().hex[:8]}",
        "parts": [{"kind": "text", "text": text}],
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store() -> InMemoryTaskStore:
    """Fresh in-memory task store for each test."""
    return InMemoryTaskStore()


@pytest.fixture()
def service(store: InMemoryTaskStore) -> A2AService:
    """A2AService backed by the in-memory store."""
    return A2AService(store=store, agent_card=make_agent_card())


@pytest.fixture()
def app(service: A2AService) -> FastAPI:
    """FastAPI app with all A2A transports mounted."""
    return make_app(service)


@pytest.fixture()
async def a2a_http(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """httpx client wired directly to the test app via ASGI transport."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=TEST_AGENT_URL,
    ) as client:
        yield client


@pytest.fixture()
def patch_session():
    """Patch build_a2a_session so tasks complete without real LLM calls.

    The patch targets the module-level name that ``A2AService._execute_agent``
    and ``_execute_agent_stream`` import at call time via
    ``from obscura.composition.a2a import build_a2a_session``.
    """
    with patch(
        "obscura.composition.a2a.build_a2a_session",
        new=_fake_build_session,
    ):
        yield
