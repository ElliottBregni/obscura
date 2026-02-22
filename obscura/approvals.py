"""Tool approval coordination for user-driven confirmation flows."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


ApprovalStatus = Literal["pending", "approved", "denied", "expired"]


@dataclass
class ToolApprovalRequest:
    """Represents one pending tool confirmation request."""

    approval_id: str
    user_id: str
    agent_id: str
    tool_use_id: str
    tool_name: str
    tool_input: dict[str, Any]
    status: ApprovalStatus = "pending"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None
    decision_reason: str | None = None
    wait_event: asyncio.Event = field(default_factory=asyncio.Event)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "tool_use_id": self.tool_use_id,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat()
            if self.resolved_at is not None
            else None,
            "decision_reason": self.decision_reason,
        }


_approvals_by_id: dict[str, ToolApprovalRequest] = {}
_approvals_lock = asyncio.Lock()


async def create_tool_approval_request(
    *,
    user_id: str,
    agent_id: str,
    tool_use_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
) -> ToolApprovalRequest:
    approval = ToolApprovalRequest(
        approval_id=f"approval-{uuid.uuid4().hex[:10]}",
        user_id=user_id,
        agent_id=agent_id,
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        tool_input=dict(tool_input),
    )
    async with _approvals_lock:
        _approvals_by_id[approval.approval_id] = approval
    return approval


async def list_tool_approval_requests(
    *,
    user_id: str,
    status: ApprovalStatus | Literal["all"] = "all",
) -> list[ToolApprovalRequest]:
    async with _approvals_lock:
        values = [
            entry for entry in _approvals_by_id.values() if entry.user_id == user_id
        ]
    if status == "all":
        return sorted(values, key=lambda entry: entry.created_at)
    filtered = [entry for entry in values if entry.status == status]
    return sorted(filtered, key=lambda entry: entry.created_at)


async def resolve_tool_approval_request(
    approval_id: str,
    *,
    user_id: str,
    approved: bool,
    reason: str | None = None,
) -> ToolApprovalRequest | None:
    async with _approvals_lock:
        approval = _approvals_by_id.get(approval_id)
        if approval is None or approval.user_id != user_id:
            return None
        if approval.status != "pending":
            return approval
        approval.status = "approved" if approved else "denied"
        approval.resolved_at = datetime.now(UTC)
        approval.decision_reason = reason
        approval.wait_event.set()
        return approval


async def wait_for_tool_approval(
    approval_id: str,
    *,
    user_id: str,
    timeout_seconds: float,
) -> bool:
    async with _approvals_lock:
        approval = _approvals_by_id.get(approval_id)
        if approval is None or approval.user_id != user_id:
            return False
        event = approval.wait_event

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
    except TimeoutError:
        async with _approvals_lock:
            current = _approvals_by_id.get(approval_id)
            if current is not None and current.status == "pending":
                current.status = "expired"
                current.resolved_at = datetime.now(UTC)
                current.decision_reason = "approval timeout"
                current.wait_event.set()
        return False

    async with _approvals_lock:
        resolved = _approvals_by_id.get(approval_id)
        if resolved is None:
            return False
        return resolved.status == "approved"


async def get_tool_approval_request(
    approval_id: str,
    *,
    user_id: str,
) -> ToolApprovalRequest | None:
    async with _approvals_lock:
        approval = _approvals_by_id.get(approval_id)
        if approval is None or approval.user_id != user_id:
            return None
        return approval


async def clear_tool_approvals() -> None:
    """Testing helper to clear in-memory approvals."""
    async with _approvals_lock:
        _approvals_by_id.clear()
