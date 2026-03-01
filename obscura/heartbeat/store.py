"""
obscura.heartbeat.store — Heartbeat data storage backends.

Provides in-memory and persistent storage for heartbeat data.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, override
from types import MappingProxyType

from obscura.heartbeat.types import Heartbeat, HealthRecord, HealthStatus

logger = logging.getLogger(__name__)


class HeartbeatStore(ABC):
    """Abstract base class for heartbeat storage backends."""

    @abstractmethod
    async def register(self, agent_id: str, expected_interval: int = 30) -> None:
        """Register a new agent for monitoring."""
        pass

    @abstractmethod
    async def unregister(self, agent_id: str) -> bool:
        """Unregister an agent from monitoring."""
        pass

    @abstractmethod
    async def save(self, heartbeat: Heartbeat) -> None:
        """Save a heartbeat from an agent."""
        pass

    @abstractmethod
    async def get_last(self, agent_id: str) -> Optional[Heartbeat]:
        """Get the last heartbeat for an agent."""
        pass

    @abstractmethod
    async def get_record(self, agent_id: str) -> Optional[HealthRecord]:
        """Get the full health record for an agent."""
        pass

    @abstractmethod
    async def list_agents(self) -> list[str]:
        """List all registered agent IDs."""
        pass

    @abstractmethod
    async def list_records(self) -> list[HealthRecord]:
        """List all health records."""
        pass

    @abstractmethod
    async def update_computed_status(self, agent_id: str, status: HealthStatus) -> None:
        """Update the computed health status for an agent."""
        pass

    @abstractmethod
    async def increment_missed_count(self, agent_id: str) -> int:
        """Increment the missed heartbeat count for an agent."""
        pass

    @abstractmethod
    async def reset_missed_count(self, agent_id: str) -> None:
        """Reset the missed heartbeat count for an agent."""
        pass


class InMemoryHeartbeatStore(HeartbeatStore):
    """
    In-memory storage for heartbeat data.

    Suitable for development and single-instance deployments.
    Data is lost on restart.
    """

    def __init__(self) -> None:
        self._records: dict[str, HealthRecord] = {}
        self._heartbeats: dict[str, Heartbeat] = {}
        logger.debug("Initialized InMemoryHeartbeatStore")

    @property
    def records(self) -> Mapping[str, HealthRecord]:
        """Read-only view of health records (testing/observability)."""
        return MappingProxyType(self._records)

    @property
    def heartbeats(self) -> Mapping[str, Heartbeat]:
        """Read-only view of latest heartbeats (testing/observability)."""
        return MappingProxyType(self._heartbeats)

    @override
    async def register(self, agent_id: str, expected_interval: int = 30) -> None:
        """Register a new agent for monitoring."""
        now = datetime.now()
        self._records[agent_id] = HealthRecord(
            agent_id=agent_id,
            expected_interval=expected_interval,
            registered_at=now,
            last_updated=now,
        )
        logger.debug(f"Registered agent {agent_id} with interval {expected_interval}s")

    @override
    async def unregister(self, agent_id: str) -> bool:
        """Unregister an agent from monitoring."""
        if agent_id in self._records:
            del self._records[agent_id]
            if agent_id in self._heartbeats:
                del self._heartbeats[agent_id]
            logger.debug(f"Unregistered agent {agent_id}")
            return True
        return False

    @override
    async def save(self, heartbeat: Heartbeat) -> None:
        """Save a heartbeat from an agent."""
        agent_id = heartbeat.agent_id
        self._heartbeats[agent_id] = heartbeat

        # Update the health record
        if agent_id in self._records:
            record = self._records[agent_id]
            record.last_heartbeat = heartbeat
            record.last_updated = datetime.now()
            record.computed_status = heartbeat.status
        else:
            # Auto-register if not already registered
            await self.register(agent_id, expected_interval=heartbeat.ttl)
            self._records[agent_id].last_heartbeat = heartbeat
            self._records[agent_id].computed_status = heartbeat.status

        logger.debug(f"Saved heartbeat from agent {agent_id}")

    @override
    async def get_last(self, agent_id: str) -> Optional[Heartbeat]:
        """Get the last heartbeat for an agent."""
        return self._heartbeats.get(agent_id)

    @override
    async def get_record(self, agent_id: str) -> Optional[HealthRecord]:
        """Get the full health record for an agent."""
        return self._records.get(agent_id)

    @override
    async def list_agents(self) -> list[str]:
        """List all registered agent IDs."""
        return list(self._records.keys())

    @override
    async def list_records(self) -> list[HealthRecord]:
        """List all health records."""
        return list(self._records.values())

    @override
    async def update_computed_status(self, agent_id: str, status: HealthStatus) -> None:
        """Update the computed health status for an agent."""
        if agent_id in self._records:
            self._records[agent_id].computed_status = status
            self._records[agent_id].last_updated = datetime.now()
            logger.debug(
                f"Updated computed status for agent {agent_id} to {status.value}"
            )

    @override
    async def increment_missed_count(self, agent_id: str) -> int:
        """Increment the missed heartbeat count for an agent."""
        if agent_id in self._records:
            self._records[agent_id].missed_count += 1
            return self._records[agent_id].missed_count
        return 0

    @override
    async def reset_missed_count(self, agent_id: str) -> None:
        """Reset the missed heartbeat count for an agent."""
        if agent_id in self._records:
            self._records[agent_id].missed_count = 0

    async def get_unhealthy_agents(self) -> list[HealthRecord]:
        """Get all agents that are not healthy."""
        return [
            record
            for record in self._records.values()
            if record.computed_status != HealthStatus.HEALTHY
        ]

    # Internal helpers used by persistence layer / tests
    def upsert_record(self, record: HealthRecord) -> None:
        """Insert or replace a health record."""
        self._records[record.agent_id] = record

    def upsert_heartbeat(self, heartbeat: Heartbeat) -> None:
        """Insert or replace a heartbeat."""
        self._heartbeats[heartbeat.agent_id] = heartbeat

    def clear(self) -> None:
        """Clear all stored data (useful for testing)."""
        self._records.clear()
        self._heartbeats.clear()


class FileHeartbeatStore(HeartbeatStore):
    """
    File-based storage for heartbeat data.

    Persists data to disk for recovery across restarts.
    """

    def __init__(self, storage_path: Path | str) -> None:
        self._storage_path = Path(storage_path)
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._memory_store = InMemoryHeartbeatStore()
        self._load_from_disk()
        logger.info(f"Initialized FileHeartbeatStore at {self._storage_path}")

    @property
    def records(self) -> Mapping[str, HealthRecord]:
        """Read-only view of persisted health records (testing/observability)."""
        return self._memory_store.records

    @property
    def heartbeats(self) -> Mapping[str, Heartbeat]:
        """Read-only view of persisted heartbeats (testing/observability)."""
        return self._memory_store.heartbeats

    def _load_from_disk(self) -> None:
        """Load existing data from disk."""
        if self._storage_path.exists():
            try:
                data = json.loads(self._storage_path.read_text())
                for record_data in data.get("records", []):
                    record = HealthRecord(
                        agent_id=record_data["agent_id"],
                        expected_interval=record_data.get("expected_interval", 30),
                        missed_count=record_data.get("missed_count", 0),
                        registered_at=datetime.fromisoformat(
                            record_data["registered_at"]
                        ),
                        last_updated=datetime.fromisoformat(
                            record_data["last_updated"]
                        ),
                        computed_status=HealthStatus(
                            record_data.get("computed_status", "unknown")
                        ),
                        alert_count=record_data.get("alert_count", 0),
                    )
                    self._memory_store.upsert_record(record)

                for heartbeat_data in data.get("heartbeats", []):
                    heartbeat = Heartbeat.from_dict(heartbeat_data)
                    self._memory_store.upsert_heartbeat(heartbeat)

                logger.info(
                    f"Loaded {len(self._memory_store.records)} records from disk"
                )
            except Exception as e:
                logger.warning(f"Failed to load data from disk: {e}")

    async def _persist_to_disk(self) -> None:
        """Persist current state to disk."""
        try:
            data: dict[str, Any] = {
                "records": [
                    {
                        "agent_id": r.agent_id,
                        "expected_interval": r.expected_interval,
                        "missed_count": r.missed_count,
                        "registered_at": r.registered_at.isoformat(),
                        "last_updated": r.last_updated.isoformat(),
                        "computed_status": r.computed_status.value,
                        "alert_count": r.alert_count,
                    }
                    for r in self._memory_store.records.values()
                ],
                "heartbeats": [
                    hb.to_dict() for hb in self._memory_store.heartbeats.values()
                ],
            }
            self._storage_path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning(f"Failed to persist data to disk: {e}")

    @override
    async def register(self, agent_id: str, expected_interval: int = 30) -> None:
        await self._memory_store.register(agent_id, expected_interval)
        await self._persist_to_disk()

    @override
    async def unregister(self, agent_id: str) -> bool:
        result = await self._memory_store.unregister(agent_id)
        await self._persist_to_disk()
        return result

    @override
    async def save(self, heartbeat: Heartbeat) -> None:
        await self._memory_store.save(heartbeat)
        await self._persist_to_disk()

    @override
    async def get_last(self, agent_id: str) -> Optional[Heartbeat]:
        return await self._memory_store.get_last(agent_id)

    @override
    async def get_record(self, agent_id: str) -> Optional[HealthRecord]:
        return await self._memory_store.get_record(agent_id)

    @override
    async def list_agents(self) -> list[str]:
        return await self._memory_store.list_agents()

    @override
    async def list_records(self) -> list[HealthRecord]:
        return await self._memory_store.list_records()

    @override
    async def update_computed_status(self, agent_id: str, status: HealthStatus) -> None:
        await self._memory_store.update_computed_status(agent_id, status)
        await self._persist_to_disk()

    @override
    async def increment_missed_count(self, agent_id: str) -> int:
        result = await self._memory_store.increment_missed_count(agent_id)
        await self._persist_to_disk()
        return result

    @override
    async def reset_missed_count(self, agent_id: str) -> None:
        await self._memory_store.reset_missed_count(agent_id)
        await self._persist_to_disk()


# Default store instance (in-memory)
_default_store: Optional[HeartbeatStore] = None


def get_default_store() -> HeartbeatStore:
    """Get or create the default heartbeat store."""
    global _default_store
    if _default_store is None:
        _default_store = InMemoryHeartbeatStore()
    return _default_store


def set_default_store(store: Optional[HeartbeatStore]) -> None:
    """Set the default heartbeat store (pass None to clear for tests)."""
    global _default_store
    _default_store = store
