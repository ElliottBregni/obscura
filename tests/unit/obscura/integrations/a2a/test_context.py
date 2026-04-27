"""Tests for A2A context management — types, store, service, and transports."""

from __future__ import annotations

# NOTE: ContextInfo/ContextState/ContextNotFoundError were replaced by the
# standard A2A Task/TaskState/TaskNotFoundError model.
# These tests need rewriting against the new API.
import pytest
pytest.skip(
    "ContextInfo/ContextState API replaced by Task/TaskState — tests need rewriting",
    allow_module_level=True,
)

from typing import Any, Literal

import pytest

from obscura.integrations.a2a.agent_card import AgentCardGenerator
from obscura.integrations.a2a.service import A2AService
from obscura.integrations.a2a.store import InMemoryTaskStore
from obscura.integrations.a2a.types import (
    A2AMessage,
    ContextInfo,
    ContextNotFoundError,
    ContextState,
    TextPart,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(
    text: str = "hello",
    role: Literal["user", "agent"] = "user",
    msg_id: str = "m1",
) -> A2AMessage:
    return A2AMessage(role=role, messageId=msg_id, parts=[TextPart(text=text)])


def _card() -> Any:
    return AgentCardGenerator("TestAgent", "https://test.local/a2a").build()


def _service(store: InMemoryTaskStore | None = None) -> A2AService:
    return A2AService(
        store=store or InMemoryTaskStore(),
        agent_card=_card(),
    )


# ---------------------------------------------------------------------------
# ContextInfo model
# ---------------------------------------------------------------------------


class TestContextInfoModel:
    def test_default_values(self) -> None:
        ctx = ContextInfo(id="ctx-1")
        assert ctx.state == ContextState.ACTIVE
        assert ctx.taskCount == 0
        assert ctx.metadata is None

    def test_serialization_roundtrip(self) -> None:
        ctx = ContextInfo(
            id="ctx-1",
            state=ContextState.CLOSED,
            metadata={"key": "value"},
            taskCount=5,
        )
        data = ctx.model_dump(mode="json")
        restored = ContextInfo.model_validate(data)
        assert restored.id == ctx.id
        assert restored.state == ContextState.CLOSED
        assert restored.metadata == {"key": "value"}
        assert restored.taskCount == 5


class TestContextNotFoundError:
    def test_error_code(self) -> None:
        err = ContextNotFoundError("ctx-missing")
        assert err.code == -32006
        assert "ctx-missing" in err.message


# ---------------------------------------------------------------------------
# InMemoryTaskStore — context lifecycle
# ---------------------------------------------------------------------------


class TestStoreContextAutoCreate:
    async def test_creating_task_creates_context(self) -> None:
        store = InMemoryTaskStore()
        await store.create_task("ctx-1", _msg())

        ctx = await store.get_context("ctx-1")
        assert ctx is not None
        assert ctx.id == "ctx-1"
        assert ctx.state == ContextState.ACTIVE
        assert ctx.taskCount == 1

    async def test_second_task_increments_count(self) -> None:
        store = InMemoryTaskStore()
        await store.create_task("ctx-1", _msg())
        await store.create_task("ctx-1", _msg(msg_id="m2"))

        ctx = await store.get_context("ctx-1")
        assert ctx is not None
        assert ctx.taskCount == 2

    async def test_unknown_context_returns_none(self) -> None:
        store = InMemoryTaskStore()
        assert await store.get_context("nope") is None


class TestStoreListContexts:
    async def test_lists_all_contexts(self) -> None:
        store = InMemoryTaskStore()
        await store.create_task("ctx-a", _msg())
        await store.create_task("ctx-b", _msg())

        contexts, cursor = await store.list_contexts()
        assert len(contexts) == 2
        assert cursor is None

    async def test_filters_by_state(self) -> None:
        store = InMemoryTaskStore()
        await store.create_task("ctx-a", _msg())
        await store.create_task("ctx-b", _msg())
        await store.update_context("ctx-b", state=ContextState.CLOSED)

        active, _ = await store.list_contexts(state=ContextState.ACTIVE)
        assert len(active) == 1
        assert active[0].id == "ctx-a"

        closed, _ = await store.list_contexts(state=ContextState.CLOSED)
        assert len(closed) == 1
        assert closed[0].id == "ctx-b"

    async def test_pagination(self) -> None:
        store = InMemoryTaskStore()
        for i in range(5):
            await store.create_task(f"ctx-{i}", _msg())

        page1, cursor = await store.list_contexts(limit=2)
        assert len(page1) == 2
        assert cursor is not None

        page2, cursor2 = await store.list_contexts(cursor=cursor, limit=2)
        assert len(page2) == 2
        assert cursor2 is not None

        page3, cursor3 = await store.list_contexts(cursor=cursor2, limit=2)
        assert len(page3) == 1
        assert cursor3 is None


class TestStoreDeleteContext:
    async def test_deletes_context_and_tasks(self) -> None:
        store = InMemoryTaskStore()
        task = await store.create_task("ctx-del", _msg())
        await store.delete_context("ctx-del")

        assert await store.get_context("ctx-del") is None
        assert await store.get_task(task.id) is None

    async def test_delete_unknown_raises(self) -> None:
        store = InMemoryTaskStore()
        with pytest.raises(ContextNotFoundError):
            await store.delete_context("nope")


class TestStoreUpdateContext:
    async def test_updates_metadata(self) -> None:
        store = InMemoryTaskStore()
        await store.create_task("ctx-upd", _msg())

        updated = await store.update_context(
            "ctx-upd",
            metadata={"agent": "molty"},
        )
        assert updated.metadata == {"agent": "molty"}

    async def test_updates_state(self) -> None:
        store = InMemoryTaskStore()
        await store.create_task("ctx-upd", _msg())

        updated = await store.update_context(
            "ctx-upd",
            state=ContextState.ARCHIVED,
        )
        assert updated.state == ContextState.ARCHIVED

    async def test_update_unknown_raises(self) -> None:
        store = InMemoryTaskStore()
        with pytest.raises(ContextNotFoundError):
            await store.update_context("nope", metadata={"x": 1})


class TestStoreContextHistory:
    async def test_aggregates_across_tasks(self) -> None:
        store = InMemoryTaskStore()
        await store.create_task("ctx-hist", _msg("first", msg_id="m1"))
        await store.create_task("ctx-hist", _msg("second", msg_id="m2"))

        history = await store.get_context_history("ctx-hist")
        texts = [p.text for m in history for p in m.parts if hasattr(p, "text")]
        assert "first" in texts
        assert "second" in texts

    async def test_respects_limit(self) -> None:
        store = InMemoryTaskStore()
        for i in range(10):
            await store.create_task("ctx-lim", _msg(f"msg-{i}", msg_id=f"m{i}"))

        history = await store.get_context_history("ctx-lim", limit=3)
        assert len(history) == 3

    async def test_unknown_context_returns_empty(self) -> None:
        store = InMemoryTaskStore()
        history = await store.get_context_history("nope")
        assert history == []


# ---------------------------------------------------------------------------
# A2AService — context methods
# ---------------------------------------------------------------------------


class TestServiceContext:
    async def test_context_get(self) -> None:
        svc = _service()
        await svc.message_send(_msg(), context_id="ctx-svc")

        ctx = await svc.context_get("ctx-svc")
        assert ctx is not None
        assert ctx.id == "ctx-svc"
        assert ctx.taskCount == 1

    async def test_context_list(self) -> None:
        svc = _service()
        await svc.message_send(_msg(), context_id="ctx-a")
        await svc.message_send(_msg(), context_id="ctx-b")

        contexts, _ = await svc.context_list()
        assert len(contexts) == 2

    async def test_context_delete(self) -> None:
        svc = _service()
        await svc.message_send(_msg(), context_id="ctx-del")

        await svc.context_delete("ctx-del")
        assert await svc.context_get("ctx-del") is None

    async def test_context_update(self) -> None:
        svc = _service()
        await svc.message_send(_msg(), context_id="ctx-upd")

        updated = await svc.context_update(
            "ctx-upd",
            metadata={"model": "kimi"},
            state=ContextState.CLOSED,
        )
        assert updated.state == ContextState.CLOSED
        assert updated.metadata == {"model": "kimi"}

    async def test_context_history(self) -> None:
        svc = _service()
        await svc.message_send(_msg("turn-1"), context_id="ctx-hist")
        await svc.message_send(_msg("turn-2"), context_id="ctx-hist")

        history = await svc.context_history("ctx-hist")
        assert len(history) >= 2

    async def test_message_send_creates_context_implicitly(self) -> None:
        svc = _service()
        task = await svc.message_send(_msg(), context_id="auto-ctx")

        ctx = await svc.context_get("auto-ctx")
        assert ctx is not None
        assert task.contextId == "auto-ctx"
