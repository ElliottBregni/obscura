"""Tests for session-scoped hooks (first-class citizen)."""

from __future__ import annotations

from pathlib import Path

import pytest

from obscura.core.supervisor.session_hooks import SessionHookManager
from obscura.core.supervisor.types import SupervisorEventKind, SupervisorHookPoint


@pytest.fixture
def hooks(tmp_path: Path) -> SessionHookManager:
    hk = SessionHookManager(
        db_path=tmp_path / "test.db",
        session_id="sess-1",
        run_id="run-1",
    )
    yield hk
    hk.close()


class TestSessionHookManager:
    def test_register_hook(self, hooks: SessionHookManager) -> None:
        entry = hooks.register(
            hook_point=SupervisorHookPoint.PRE_TOOL_EXECUTION,
            hook_type="before",
            handler_ref="audit_tool",
            handler=lambda ctx: ctx,
        )
        assert entry.hook_point == SupervisorHookPoint.PRE_TOOL_EXECUTION
        assert hooks.hook_count == 1

    @pytest.mark.asyncio
    async def test_fire_before_hook(self, hooks: SessionHookManager) -> None:
        called: list[dict] = []

        def handler(ctx: dict) -> dict:
            called.append(ctx)
            return ctx

        hooks.register(
            hook_point=SupervisorHookPoint.PRE_TOOL_EXECUTION,
            hook_type="before",
            handler_ref="audit",
            handler=handler,
        )

        result = await hooks.fire_before(
            SupervisorHookPoint.PRE_TOOL_EXECUTION,
            {"tool_name": "bash"},
        )
        assert result is not None
        assert result["tool_name"] == "bash"
        assert len(called) == 1

    @pytest.mark.asyncio
    async def test_fire_before_suppress(self, hooks: SessionHookManager) -> None:
        """Before hook returning None suppresses the action."""
        hooks.register(
            hook_point=SupervisorHookPoint.PRE_TOOL_EXECUTION,
            hook_type="before",
            handler_ref="blocker",
            handler=lambda ctx: None,
        )

        result = await hooks.fire_before(
            SupervisorHookPoint.PRE_TOOL_EXECUTION,
            {"tool_name": "dangerous"},
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_fire_after_hook(self, hooks: SessionHookManager) -> None:
        called: list[dict] = []

        hooks.register(
            hook_point=SupervisorHookPoint.POST_TOOL_EXECUTION,
            hook_type="after",
            handler_ref="logger",
            handler=lambda ctx: called.append(ctx),
        )

        await hooks.fire_after(
            SupervisorHookPoint.POST_TOOL_EXECUTION,
            {"tool_name": "bash", "result": "ok"},
        )
        assert len(called) == 1

    @pytest.mark.asyncio
    async def test_async_hook(self, hooks: SessionHookManager) -> None:
        called: list[bool] = []

        async def async_handler(ctx: dict) -> dict:
            called.append(True)
            return ctx

        hooks.register(
            hook_point=SupervisorHookPoint.PRE_MODEL_TURN,
            hook_type="before",
            handler_ref="async_audit",
            handler=async_handler,
        )

        result = await hooks.fire_before(SupervisorHookPoint.PRE_MODEL_TURN, {})
        assert result is not None
        assert len(called) == 1

    def test_priority_ordering(self, hooks: SessionHookManager) -> None:
        order: list[int] = []

        hooks.register(
            hook_point=SupervisorHookPoint.PRE_TOOL_EXECUTION,
            hook_type="before",
            handler_ref="second",
            handler=lambda ctx: (order.append(2), ctx)[-1],
            priority=20,
        )
        hooks.register(
            hook_point=SupervisorHookPoint.PRE_TOOL_EXECUTION,
            hook_type="before",
            handler_ref="first",
            handler=lambda ctx: (order.append(1), ctx)[-1],
            priority=10,
        )

        import asyncio
        asyncio.get_event_loop().run_until_complete(
            hooks.fire_before(SupervisorHookPoint.PRE_TOOL_EXECUTION, {})
        )
        assert order == [1, 2]

    def test_persistence_and_reload(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"

        # Register hooks
        h1 = SessionHookManager(db_path=db, session_id="sess-1")
        h1.register(
            hook_point=SupervisorHookPoint.PRE_TOOL_EXECUTION,
            hook_type="before",
            handler_ref="audit",
            persist=True,
        )
        h1.close()

        # Reload in new manager
        h2 = SessionHookManager(db_path=db, session_id="sess-1")
        count = h2.load_from_db()
        assert count == 1
        assert h2.hook_count == 1
        h2.close()

    def test_bind_handler_on_resume(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"

        # Register without handler
        h1 = SessionHookManager(db_path=db, session_id="sess-1")
        h1.register(
            hook_point=SupervisorHookPoint.PRE_TOOL_EXECUTION,
            hook_type="before",
            handler_ref="audit",
        )
        h1.close()

        # Reload and bind handler
        h2 = SessionHookManager(db_path=db, session_id="sess-1")
        called: list[bool] = []
        h2.bind_handler("audit", lambda ctx: (called.append(True), ctx)[-1])
        h2.load_from_db()

        import asyncio
        asyncio.get_event_loop().run_until_complete(
            h2.fire_before(SupervisorHookPoint.PRE_TOOL_EXECUTION, {})
        )
        assert len(called) == 1
        h2.close()

    def test_events_emitted(self, hooks: SessionHookManager) -> None:
        hooks.register(
            hook_point=SupervisorHookPoint.PRE_TOOL_EXECUTION,
            hook_type="before",
            handler_ref="test",
        )
        assert len(hooks.events) >= 1
        assert hooks.events[0].kind == SupervisorEventKind.HOOK_REGISTERED
