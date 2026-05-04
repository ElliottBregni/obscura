"""Tool approval coordination for user-driven confirmation flows.

State is the :class:`ApprovalRecord` Pydantic model from
``obscura.core.models.lifecycle``. Each pending approval is paired with
an :class:`asyncio.Event` (kept in a side-table) so callers can ``await``
on a decision without coupling the wait primitive into the persisted
record shape.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from obscura.core.enums.lifecycle import ApprovalStatus as ApprovalStatus
from obscura.core.models.lifecycle import ApprovalRecord

logger = logging.getLogger(__name__)


# Re-export under the historical dataclass name so existing imports keep
# resolving while consumers migrate. The runtime type is the Pydantic model.
ToolApprovalRequest = ApprovalRecord


_approvals_by_id: dict[str, ApprovalRecord] = {}
_wait_events: dict[str, asyncio.Event] = {}
_approvals_lock = asyncio.Lock()


async def create_tool_approval_request(
    *,
    user_id: str,
    agent_id: str,
    tool_use_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
) -> ApprovalRecord:
    now = datetime.now(UTC)
    approval = ApprovalRecord(
        id=f"approval-{uuid.uuid4().hex[:10]}",
        status=ApprovalStatus.PENDING,
        status_changed_at=now,
        created_at=now,
        updated_at=now,
        user_id=user_id,
        agent_id=agent_id,
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        tool_input=dict(tool_input),
    )
    async with _approvals_lock:
        _approvals_by_id[approval.id] = approval
        _wait_events[approval.id] = asyncio.Event()
    return approval


async def list_tool_approval_requests(
    *,
    user_id: str,
    status: ApprovalStatus | Literal["all"] | str = "all",
) -> list[ApprovalRecord]:
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
) -> ApprovalRecord | None:
    async with _approvals_lock:
        approval = _approvals_by_id.get(approval_id)
        if approval is None or approval.user_id != user_id:
            return None
        if approval.status != ApprovalStatus.PENDING:
            return approval
        now = datetime.now(UTC)
        approval.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.DENIED
        approval.status_changed_at = now
        approval.updated_at = now
        approval.resolved_at = now
        approval.decision_reason = reason
        event = _wait_events.get(approval_id)
        if event is not None:
            event.set()
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
        event = _wait_events.get(approval_id)
        if event is None:
            event = asyncio.Event()
            _wait_events[approval_id] = event

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout_seconds)
    except TimeoutError:
        logger.debug("suppressed exception in wait_for_tool_approval", exc_info=True)
        async with _approvals_lock:
            current = _approvals_by_id.get(approval_id)
            if current is not None and current.status == ApprovalStatus.PENDING:
                now = datetime.now(UTC)
                current.status = ApprovalStatus.EXPIRED
                current.status_changed_at = now
                current.updated_at = now
                current.resolved_at = now
                current.decision_reason = "approval timeout"
                expired_event = _wait_events.get(approval_id)
                if expired_event is not None:
                    expired_event.set()
        return False

    async with _approvals_lock:
        resolved = _approvals_by_id.get(approval_id)
        if resolved is None:
            return False
        return resolved.status == ApprovalStatus.APPROVED


async def get_tool_approval_request(
    approval_id: str,
    *,
    user_id: str,
) -> ApprovalRecord | None:
    async with _approvals_lock:
        approval = _approvals_by_id.get(approval_id)
        if approval is None or approval.user_id != user_id:
            return None
        return approval


async def clear_tool_approvals() -> None:
    """Testing helper to clear in-memory approvals."""
    async with _approvals_lock:
        _approvals_by_id.clear()
        _wait_events.clear()
