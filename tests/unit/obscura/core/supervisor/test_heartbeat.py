"""Tests for session-scoped heartbeat (first-class citizen)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from obscura.core.supervisor.heartbeat import (
    SessionHeartbeatManager,
    get_heartbeats_for_run,
)
from obscura.core.supervisor.types import SupervisorEventKind, SupervisorState


@pytest.fixture
def hb_manager(tmp_path: Path) -> SessionHeartbeatManager:
    hb = SessionHeartbeatManager(
        db_path=tmp_path / "test.db",
        session_id="sess-1",
        run_id="run-1",
        interval=0.1,  # fast for testing
    )
    yield hb
    hb.close()


class TestSessionHeartbeatManager:
    @pytest.mark.asyncio
    async def test_start_stop(self, hb_manager: SessionHeartbeatManager) -> None:
        assert hb_manager.is_running is False
        await hb_manager.start()
        assert hb_manager.is_running is True
        await asyncio.sleep(0.3)
        await hb_manager.stop()
        assert hb_manager.is_running is False
        assert hb_manager.beat_count >= 2  # at least 2 beats + final

    @pytest.mark.asyncio
    async def test_heartbeat_events(self, hb_manager: SessionHeartbeatManager) -> None:
        await hb_manager.start()
        await asyncio.sleep(0.25)
        await hb_manager.stop()
        events = hb_manager.events
        assert len(events) >= 2
        assert all(e.kind == SupervisorEventKind.HEARTBEAT for e in events)

    @pytest.mark.asyncio
    async def test_state_update(self, hb_manager: SessionHeartbeatManager) -> None:
        hb_manager.update_state(SupervisorState.RUNNING_MODEL, turn=3)
        await hb_manager.start()
        await asyncio.sleep(0.15)
        await hb_manager.stop()
        events = hb_manager.events
        assert any(
            e.payload.get("state") == "running_model" and e.payload.get("turn_number") == 3
            for e in events
        )

    @pytest.mark.asyncio
    async def test_callback_invoked(self, hb_manager: SessionHeartbeatManager) -> None:
        called: list[bool] = []
        hb_manager.on_tick(lambda hb: called.append(True))
        await hb_manager.start()
        await asyncio.sleep(0.15)
        await hb_manager.stop()
        assert len(called) >= 1

    @pytest.mark.asyncio
    async def test_persisted_to_db(self, tmp_path: Path) -> None:
        db = tmp_path / "test.db"
        hb = SessionHeartbeatManager(
            db_path=db, session_id="sess-1", run_id="run-1", interval=0.1,
        )
        await hb.start()
        await asyncio.sleep(0.25)
        await hb.stop()
        hb.close()

        # Query DB
        beats = get_heartbeats_for_run(db, "run-1")
        assert len(beats) >= 2
        assert all(b.session_id == "sess-1" for b in beats)
        assert all(b.run_id == "run-1" for b in beats)

    @pytest.mark.asyncio
    async def test_idempotent_start(self, hb_manager: SessionHeartbeatManager) -> None:
        await hb_manager.start()
        await hb_manager.start()  # should not create a second task
        await asyncio.sleep(0.15)
        await hb_manager.stop()

    @pytest.mark.asyncio
    async def test_idempotent_stop(self, hb_manager: SessionHeartbeatManager) -> None:
        await hb_manager.stop()  # should not raise
        await hb_manager.stop()
