"""Tests for sdk.heartbeat.store — InMemoryHeartbeatStore and FileHeartbeatStore."""
import asyncio
import pytest
import json
import tempfile
from pathlib import Path
from datetime import datetime

from sdk.heartbeat.store import (
    InMemoryHeartbeatStore,
    FileHeartbeatStore,
    get_default_store,
    set_default_store,
)
from sdk.heartbeat.types import Heartbeat, HealthStatus


class TestInMemoryHeartbeatStore:
    @pytest.mark.asyncio
    async def test_register_and_list(self):
        store = InMemoryHeartbeatStore()
        await store.register("a1", 30)
        agents = await store.list_agents()
        assert "a1" in agents

    @pytest.mark.asyncio
    async def test_unregister(self):
        store = InMemoryHeartbeatStore()
        await store.register("a1")
        assert await store.unregister("a1") is True
        assert await store.unregister("a1") is False

    @pytest.mark.asyncio
    async def test_save_and_get_last(self):
        store = InMemoryHeartbeatStore()
        hb = Heartbeat(
            agent_id="a1",
            timestamp=datetime.now(),
            status=HealthStatus.HEALTHY,
        )
        await store.save(hb)
        last = await store.get_last("a1")
        assert last is not None
        assert last.agent_id == "a1"

    @pytest.mark.asyncio
    async def test_save_auto_registers(self):
        store = InMemoryHeartbeatStore()
        hb = Heartbeat(
            agent_id="new-agent",
            timestamp=datetime.now(),
            status=HealthStatus.HEALTHY,
            ttl=60,
        )
        await store.save(hb)
        record = await store.get_record("new-agent")
        assert record is not None
        assert record.last_heartbeat is hb

    @pytest.mark.asyncio
    async def test_save_updates_existing_record(self):
        store = InMemoryHeartbeatStore()
        await store.register("a1", 30)
        hb = Heartbeat(
            agent_id="a1",
            timestamp=datetime.now(),
            status=HealthStatus.WARNING,
        )
        await store.save(hb)
        record = await store.get_record("a1")
        assert record is not None
        assert record.computed_status == HealthStatus.WARNING

    @pytest.mark.asyncio
    async def test_get_record_none(self):
        store = InMemoryHeartbeatStore()
        assert await store.get_record("nonexistent") is None

    @pytest.mark.asyncio
    async def test_list_records(self):
        store = InMemoryHeartbeatStore()
        await store.register("a1")
        await store.register("a2")
        records = await store.list_records()
        assert len(records) == 2

    @pytest.mark.asyncio
    async def test_update_computed_status(self):
        store = InMemoryHeartbeatStore()
        await store.register("a1")
        await store.update_computed_status("a1", HealthStatus.CRITICAL)
        record = await store.get_record("a1")
        assert record is not None
        assert record.computed_status == HealthStatus.CRITICAL

    @pytest.mark.asyncio
    async def test_update_computed_status_unknown_agent(self):
        store = InMemoryHeartbeatStore()
        await store.update_computed_status("unknown", HealthStatus.HEALTHY)

    @pytest.mark.asyncio
    async def test_increment_missed_count(self):
        store = InMemoryHeartbeatStore()
        await store.register("a1")
        count = await store.increment_missed_count("a1")
        assert count == 1
        count = await store.increment_missed_count("a1")
        assert count == 2

    @pytest.mark.asyncio
    async def test_increment_missed_count_unknown(self):
        store = InMemoryHeartbeatStore()
        count = await store.increment_missed_count("unknown")
        assert count == 0

    @pytest.mark.asyncio
    async def test_reset_missed_count(self):
        store = InMemoryHeartbeatStore()
        await store.register("a1")
        await store.increment_missed_count("a1")
        await store.reset_missed_count("a1")
        record = await store.get_record("a1")
        assert record is not None
        assert record.missed_count == 0

    @pytest.mark.asyncio
    async def test_reset_missed_count_unknown(self):
        store = InMemoryHeartbeatStore()
        await store.reset_missed_count("unknown")

    @pytest.mark.asyncio
    async def test_get_unhealthy_agents(self):
        store = InMemoryHeartbeatStore()
        await store.register("a1")
        await store.register("a2")
        await store.update_computed_status("a1", HealthStatus.CRITICAL)
        unhealthy = await store.get_unhealthy_agents()
        assert len(unhealthy) == 2

    def test_clear(self):
        store = InMemoryHeartbeatStore()
        # populate via public API
        asyncio.run(store.register("a1"))
        store.clear()
        assert len(store.records) == 0

    @pytest.mark.asyncio
    async def test_unregister_with_heartbeat(self):
        store = InMemoryHeartbeatStore()
        hb = Heartbeat(agent_id="a1", timestamp=datetime.now(), status=HealthStatus.HEALTHY)
        await store.save(hb)
        await store.unregister("a1")
        assert await store.get_last("a1") is None


class TestFileHeartbeatStore:
    @pytest.mark.asyncio
    async def test_register_persists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "heartbeats.json"
            store = FileHeartbeatStore(path)
            await store.register("a1", 30)
            assert path.exists()
            data = json.loads(path.read_text())
            assert len(data["records"]) == 1

    @pytest.mark.asyncio
    async def test_save_persists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "heartbeats.json"
            store = FileHeartbeatStore(path)
            hb = Heartbeat(agent_id="a1", timestamp=datetime.now(), status=HealthStatus.HEALTHY)
            await store.save(hb)
            data = json.loads(path.read_text())
            assert len(data["heartbeats"]) == 1

    @pytest.mark.asyncio
    async def test_load_from_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "heartbeats.json"
            data: dict[str, list[dict[str, object]]] = {
                "records": [{
                    "agent_id": "a1",
                    "expected_interval": 30,
                    "missed_count": 0,
                    "registered_at": datetime.now().isoformat(),
                    "last_updated": datetime.now().isoformat(),
                    "computed_status": "healthy",
                    "alert_count": 0,
                }],
                "heartbeats": [{
                    "agent_id": "a1",
                    "timestamp": datetime.now().isoformat(),
                    "status": "healthy",
                    "metrics": {},
                    "ttl": 30,
                    "version": "0.1.0",
                    "tags": [],
                }],
            }
            path.write_text(json.dumps(data))

            store = FileHeartbeatStore(path)
            agents = await store.list_agents()
            assert "a1" in agents

    @pytest.mark.asyncio
    async def test_load_from_disk_bad_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "heartbeats.json"
            path.write_text("not json")
            store = FileHeartbeatStore(path)
            agents = await store.list_agents()
            assert agents == []

    @pytest.mark.asyncio
    async def test_unregister_persists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "heartbeats.json"
            store = FileHeartbeatStore(path)
            await store.register("a1")
            await store.unregister("a1")
            data = json.loads(path.read_text())
            assert len(data["records"]) == 0

    @pytest.mark.asyncio
    async def test_delegates_to_memory_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "heartbeats.json"
            store = FileHeartbeatStore(path)
            await store.register("a1")
            record = await store.get_record("a1")
            assert record is not None
            records = await store.list_records()
            assert len(records) == 1
            await store.update_computed_status("a1", HealthStatus.WARNING)
            count = await store.increment_missed_count("a1")
            assert count == 1
            await store.reset_missed_count("a1")
            last = await store.get_last("a1")
            assert last is None
            agents = await store.list_agents()
            assert "a1" in agents


class TestDefaultStore:
    def test_get_default_store(self):
        store = get_default_store()
        assert isinstance(store, InMemoryHeartbeatStore)

    def test_set_default_store(self):
        custom = InMemoryHeartbeatStore()
        set_default_store(custom)
        assert get_default_store() is custom
        set_default_store(None)
