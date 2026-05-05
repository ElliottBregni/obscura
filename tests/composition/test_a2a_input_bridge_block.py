"""Tests for `install_a2a_input_bridge` — A2A INPUT_REQUIRED bridge.

Covers:
- Surface guard (only A2A wires callbacks)
- All three callbacks land on session.host_callbacks when factories given
- A2AService._make_ask_user free-text answer flow via _resume_task
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from obscura.composition.blocks.a2a_input_bridge import install_a2a_input_bridge
from obscura.composition.session import (
    AgentSession,
    SessionConfig,
    new_session_id,
)


class _StubClient:
    def __init__(self) -> None:
        from obscura.core.tools import ToolRegistry

        self._tool_registry = ToolRegistry()
        self._backend = self

    def register_tool(self, spec: Any) -> None:
        pass


def _make_session(*, surface: str = "a2a") -> AgentSession:
    return AgentSession(
        session_id=new_session_id(),
        surface=surface,  # type: ignore[arg-type]
        config=SessionConfig(),
        client=_StubClient(),  # type: ignore[arg-type]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_a2a_surface_skipped() -> None:
    """REPL/API/MCP shouldn't pick up A2A's INPUT_REQUIRED-backed callbacks."""
    for surface in ("repl", "api", "mcp_server"):
        session = _make_session(surface=surface)
        await install_a2a_input_bridge(
            session,
            ask_user=AsyncMock(),
            plan_approval=AsyncMock(),
        )
        assert "ask_user_callback" not in session.host_callbacks
        assert "plan_approval_callback" not in session.host_callbacks


@pytest.mark.unit
@pytest.mark.asyncio
async def test_wires_provided_callbacks() -> None:
    session = _make_session()
    fake_ask = AsyncMock(return_value="user said hi")
    fake_plan = AsyncMock(return_value=True)

    await install_a2a_input_bridge(
        session,
        ask_user=fake_ask,
        plan_approval=fake_plan,
    )

    assert session.host_callbacks["ask_user_callback"] is fake_ask
    assert session.host_callbacks["plan_approval_callback"] is fake_plan
    # permission_mode is always wired (logging stub) on a2a surface
    assert "permission_mode_callback" in session.host_callbacks


@pytest.mark.unit
@pytest.mark.asyncio
async def test_skips_unset_factories() -> None:
    """If only ask_user is provided, plan_approval stays unwired."""
    session = _make_session()
    fake_ask = AsyncMock()

    await install_a2a_input_bridge(session, ask_user=fake_ask)

    assert "ask_user_callback" in session.host_callbacks
    assert "plan_approval_callback" not in session.host_callbacks


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a2a_service_ask_user_captures_free_text() -> None:
    """End-to-end: A2AService._make_ask_user parks the task; _resume_task
    captures a free-text answer and wakes the callback."""
    from obscura.integrations.a2a.agent_card import AgentCardGenerator
    from obscura.integrations.a2a.service import A2AService
    from obscura.integrations.a2a.store import InMemoryTaskStore
    from obscura.integrations.a2a.types import A2AMessage, TextPart
    from obscura.core.enums.protocol import A2ARole

    from obscura.core.enums.protocol import A2ATaskState

    card = (
        AgentCardGenerator(name="t", url="http://t")
        .with_bearer_auth()
        .with_provider("p", "http://p")
        .build()
    )
    store = InMemoryTaskStore()
    svc = A2AService(store=store, agent_card=card)
    task = await store.create_task(
        context_id="ctx",
        initial_message=A2AMessage(
            role=A2ARole.USER,
            messageId="m-1",
            parts=[TextPart(text="hello")],
        ),
    )
    # Tasks start as PENDING; need to transition to WORKING before
    # INPUT_REQUIRED can be entered (matches real agent execution flow)
    await store.transition(task.id, A2ATaskState.WORKING)

    ask_user = svc._make_ask_user(task.id)

    async def _ask_then_resume() -> str:
        # Schedule the resume after a short delay so ask_user can park first
        async def _later() -> None:
            await asyncio.sleep(0.05)
            reply = A2AMessage(
                role=A2ARole.USER,
                messageId="m-2",
                parts=[TextPart(text="42 widgets please")],
            )
            await svc._resume_task(task.id, reply)

        asyncio.create_task(_later())
        return await ask_user("How many widgets?")

    result = await _ask_then_resume()
    assert result == "42 widgets please"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a2a_service_plan_approval_yes_no() -> None:
    """Plan approval still parses y/n approval correctly."""
    from obscura.integrations.a2a.agent_card import AgentCardGenerator
    from obscura.integrations.a2a.service import A2AService
    from obscura.integrations.a2a.store import InMemoryTaskStore
    from obscura.integrations.a2a.types import A2AMessage, TextPart
    from obscura.core.enums.protocol import A2ARole

    from obscura.core.enums.protocol import A2ATaskState

    card = (
        AgentCardGenerator(name="t", url="http://t")
        .with_bearer_auth()
        .with_provider("p", "http://p")
        .build()
    )
    store = InMemoryTaskStore()
    svc = A2AService(store=store, agent_card=card)
    task = await store.create_task(
        context_id="ctx",
        initial_message=A2AMessage(
            role=A2ARole.USER,
            messageId="m-1",
            parts=[TextPart(text="...")],
        ),
    )
    await store.transition(task.id, A2ATaskState.WORKING)

    plan_approval = svc._make_plan_approval(task.id)

    async def _gate(answer_text: str) -> bool:
        async def _later() -> None:
            await asyncio.sleep(0.05)
            reply = A2AMessage(
                role=A2ARole.USER,
                messageId=f"m-{answer_text}",
                parts=[TextPart(text=answer_text)],
            )
            await svc._resume_task(task.id, reply)

        asyncio.create_task(_later())
        return await plan_approval("Implement the plan?")

    assert await _gate("yes") is True
    # Reset pending state before the second test (previous _resume_task cleared)
    plan_approval2 = svc._make_plan_approval(task.id)
    async def _gate2(answer_text: str) -> bool:
        async def _later() -> None:
            await asyncio.sleep(0.05)
            reply = A2AMessage(
                role=A2ARole.USER,
                messageId=f"m-{answer_text}",
                parts=[TextPart(text=answer_text)],
            )
            await svc._resume_task(task.id, reply)
        asyncio.create_task(_later())
        return await plan_approval2(answer_text)

    assert await _gate2("nope") is False
