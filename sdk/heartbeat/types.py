"""
sdk.heartbeat.types — Core types for the heartbeat monitoring system.

Defines HealthStatus, Heartbeat, HealthCheck and related data structures
used by the heartbeat monitoring infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Optional


class HealthStatus(Enum):
    """Agent health status states."""
    HEALTHY = "healthy"
    WARNING = "warning"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


@dataclass
class SystemMetrics:
    """System metrics collected by the heartbeat client."""
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    disk_usage_percent: float = 0.0
    queue_depth: int = 0
    active_tasks: int = 0
    uptime_seconds: float = 0.0
    
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SystemMetrics:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Heartbeat:
    """
    Heartbeat message sent by agents to report their health status.
    
    Attributes:
        agent_id: Unique identifier for the agent
        timestamp: When the heartbeat was generated
        status: Current health status
        metrics: System metrics (CPU, memory, etc.)
        message: Optional status message or error details
        ttl: Time-to-live in seconds (how long until considered stale)
        version: Agent version string
        tags: Optional tags for categorization
    """
    agent_id: str
    timestamp: datetime
    status: HealthStatus
    metrics: SystemMetrics = field(default_factory=SystemMetrics)
    message: Optional[str] = None
    ttl: int = 30  # seconds
    version: str = "0.1.0"
    tags: list[str] = field(default_factory=list)
    
    def to_dict(self) -> dict[str, Any]:
        """Convert heartbeat to dictionary for serialization."""
        return {
            "agent_id": self.agent_id,
            "timestamp": self.timestamp.isoformat(),
            "status": self.status.value,
            "metrics": self.metrics.to_dict(),
            "message": self.message,
            "ttl": self.ttl,
            "version": self.version,
            "tags": self.tags,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Heartbeat:
        """Create heartbeat from dictionary."""
        return cls(
            agent_id=data["agent_id"],
            timestamp=datetime.fromisoformat(data["timestamp"]),
            status=HealthStatus(data["status"]),
            metrics=SystemMetrics.from_dict(data.get("metrics", {})),
            message=data.get("message"),
            ttl=data.get("ttl", 30),
            version=data.get("version", "0.1.0"),
            tags=data.get("tags", []),
        )


@dataclass
class HealthCheck:
    """
    Definition of a health check that can be performed.
    
    Attributes:
        name: Unique identifier for this check
        check_fn: Function that returns HealthStatus
        interval: How often to run this check (seconds)
        timeout: Maximum time to wait for check (seconds)
        description: Human-readable description
    """
    name: str
    check_fn: Callable[[], HealthStatus] = field(compare=False)
    interval: int = 30
    timeout: int = 10
    description: str = ""


@dataclass
class HealthRecord:
    """
    Stored health record for an agent.
    
    Includes the latest heartbeat and computed health status.
    """
    agent_id: str
    last_heartbeat: Optional[Heartbeat] = None
    computed_status: HealthStatus = HealthStatus.UNKNOWN
    expected_interval: int = 30  # seconds between heartbeats
    missed_count: int = 0
    registered_at: datetime = field(default_factory=lambda: datetime.now())
    last_updated: datetime = field(default_factory=lambda: datetime.now())
    alert_count: int = 0
    
    def to_dict(self) -> dict[str, Any]:
        """Convert health record to dictionary."""
        return {
            "agent_id": self.agent_id,
            "last_heartbeat": self.last_heartbeat.to_dict() if self.last_heartbeat else None,
            "computed_status": self.computed_status.value,
            "expected_interval": self.expected_interval,
            "missed_count": self.missed_count,
            "registered_at": self.registered_at.isoformat(),
            "last_updated": self.last_updated.isoformat(),
            "alert_count": self.alert_count,
        }


@dataclass
class Alert:
    """
    Alert generated when health status changes or thresholds are crossed.
    
    Attributes:
        alert_id: Unique identifier
        agent_id: Agent that triggered the alert
        severity: Alert severity level
        status: Health status that triggered the alert
        message: Human-readable alert message
        timestamp: When the alert was generated
        acknowledged: Whether the alert has been acknowledged
    """
    alert_id: str
    agent_id: str
    severity: HealthStatus
    status: HealthStatus
    message: str
    timestamp: datetime
    acknowledged: bool = False
    acknowledged_by: Optional[str] = None
    acknowledged_at: Optional[datetime] = None
    
    def to_dict(self) -> dict[str, Any]:
        """Convert alert to dictionary."""
        return {
            "alert_id": self.alert_id,
            "agent_id": self.agent_id,
            "severity": self.severity.value,
            "status": self.status.value,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(),
            "acknowledged": self.acknowledged,
            "acknowledged_by": self.acknowledged_by,
            "acknowledged_at": self.acknowledged_at.isoformat() if self.acknowledged_at else None,
        }


class HealthStatusTransition:
    """Tracks health status transitions for an agent."""
    
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.transitions: list[tuple[datetime, HealthStatus, HealthStatus]] = []
        self.current_status = HealthStatus.UNKNOWN
    
    def record_transition(self, from_status: HealthStatus, to_status: HealthStatus) -> bool:
        """Record a status transition if it's actually a change."""
        if from_status != to_status:
            self.transitions.append((datetime.now(), from_status, to_status))
            self.current_status = to_status
            return True
        return False
    
    def get_history(self) -> list[dict[str, Any]]:
        """Get transition history as dictionaries."""
        return [
            {
                "timestamp": ts.isoformat(),
                "from": from_s.value,
                "to": to_s.value,
            }
            for ts, from_s, to_s in self.transitions
        ]
