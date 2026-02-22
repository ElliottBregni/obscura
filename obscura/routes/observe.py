"""Routes: runtime observation snapshot + SSE stream."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, AsyncIterator, cast

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from obscura.auth.models import AuthenticatedUser
from obscura.auth.rbac import AGENT_READ_ROLES, require_any_role
from obscura.approvals import list_tool_approval_requests

router = APIRouter(prefix="/api/v1", tags=["observe"])


def _parse_iso_datetime(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class _ObservedAgentState:
    agent_id: str
    name: str
    status: str
    updated_at: datetime
    iteration_count: int
    error_message: str | None

    def signature(self) -> tuple[str, str, int, str | None]:
        return (
            self.status,
            self.updated_at.isoformat(),
            self.iteration_count,
            self.error_message,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "status": self.status,
            "updated_at": self.updated_at.isoformat(),
            "iteration_count": self.iteration_count,
            "error_message": self.error_message,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> _ObservedAgentState | None:
        agent_id = payload.get("agent_id")
        name = payload.get("name")
        status = payload.get("status")
        updated_raw = payload.get("updated_at")
        if not isinstance(agent_id, str) or not agent_id.strip():
            return None
        if not isinstance(name, str) or not isinstance(status, str):
            return None
        if not isinstance(updated_raw, str):
            return None
        updated_at = _parse_iso_datetime(updated_raw)
        if updated_at is None:
            return None
        iteration_raw = payload.get("iteration_count", 0)
        iteration_count = iteration_raw if isinstance(iteration_raw, int) else 0
        error_message = payload.get("error_message")
        if error_message is not None and not isinstance(error_message, str):
            error_message = str(error_message)
        return cls(
            agent_id=agent_id,
            name=name,
            status=status,
            updated_at=updated_at,
            iteration_count=iteration_count,
            error_message=error_message,
        )


def _collect_states(
    *,
    user: AuthenticatedUser,
    namespace: str,
) -> list[_ObservedAgentState]:
    from obscura.memory import MemoryStore

    store = MemoryStore.for_user(user)
    states: list[_ObservedAgentState] = []
    for key in store.list_keys(namespace=namespace):
        if not key.key.startswith("agent_state_"):
            continue
        payload = store.get(key.key, namespace=namespace)
        if not isinstance(payload, dict):
            continue
        payload_dict = cast(dict[Any, Any], payload)
        typed_payload: dict[str, Any] = {}
        for raw_key, raw_value in payload_dict.items():
            key_name = raw_key if isinstance(raw_key, str) else str(raw_key)
            typed_payload[key_name] = raw_value
        state = _ObservedAgentState.from_payload(typed_payload)
        if state is not None:
            states.append(state)
    return sorted(states, key=lambda entry: (entry.updated_at, entry.agent_id))


def _stale_ids(
    states: list[_ObservedAgentState],
    *,
    now: datetime,
    stale_seconds: float,
) -> list[str]:
    stale: list[str] = []
    for state in states:
        if state.status not in {"RUNNING", "WAITING"}:
            continue
        if (now - state.updated_at).total_seconds() >= stale_seconds:
            stale.append(state.agent_id)
    return stale


@router.get("/observe")
async def observe_snapshot(
    namespace: str = "agent:runtime",
    stale_seconds: float = 20.0,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> JSONResponse:
    """Return one observation snapshot for the current user."""
    now = datetime.now(UTC)
    threshold = max(1.0, stale_seconds)
    states = _collect_states(user=user, namespace=namespace)
    stale_agent_ids = _stale_ids(states, now=now, stale_seconds=threshold)
    pending_approvals = await list_tool_approval_requests(
        user_id=user.user_id,
        status="pending",
    )
    return JSONResponse(
        content={
            "timestamp": now.isoformat(),
            "namespace": namespace,
            "count": len(states),
            "stale_seconds": threshold,
            "stale_agent_ids": stale_agent_ids,
            "states": [state.to_dict() for state in states],
            "pending_tool_approvals": [entry.to_dict() for entry in pending_approvals],
        }
    )


@router.get("/observe/stream")
async def observe_stream(
    namespace: str = "agent:runtime",
    interval_seconds: float = 1.0,
    stale_seconds: float = 20.0,
    once: bool = False,
    user: AuthenticatedUser = Depends(require_any_role(*AGENT_READ_ROLES)),
) -> EventSourceResponse:
    """Stream observation updates as Server-Sent Events."""
    interval = max(0.1, interval_seconds)
    threshold = max(1.0, stale_seconds)

    async def _event_generator() -> AsyncIterator[dict[str, str]]:
        previous_by_id: dict[str, tuple[str, str, int, str | None]] = {}
        stale_alerts: set[tuple[str, str]] = set()
        seen_pending_approvals: set[str] = set()

        while True:
            now = datetime.now(UTC)
            states = _collect_states(user=user, namespace=namespace)
            stale_agent_ids = _stale_ids(states, now=now, stale_seconds=threshold)
            pending_approvals = await list_tool_approval_requests(
                user_id=user.user_id,
                status="pending",
            )
            payload = {
                "timestamp": now.isoformat(),
                "namespace": namespace,
                "count": len(states),
                "states": [state.to_dict() for state in states],
                "stale_agent_ids": stale_agent_ids,
                "pending_tool_approvals": [
                    entry.to_dict() for entry in pending_approvals
                ],
            }
            yield {
                "event": "snapshot",
                "data": json.dumps(payload),
            }

            stale_set = set(stale_agent_ids)
            for state in states:
                signature = state.signature()
                if previous_by_id.get(state.agent_id) != signature:
                    yield {
                        "event": "agent_state",
                        "data": json.dumps(state.to_dict()),
                    }
                if state.agent_id in stale_set:
                    stale_key = (state.agent_id, state.updated_at.isoformat())
                    if stale_key not in stale_alerts:
                        stale_alerts.add(stale_key)
                        age_seconds = (now - state.updated_at).total_seconds()
                        yield {
                            "event": "stalled",
                            "data": json.dumps(
                                {
                                    "agent_id": state.agent_id,
                                    "status": state.status,
                                    "age_seconds": age_seconds,
                                }
                            ),
                        }

            current_ids = {state.agent_id for state in states}
            removed_ids = set(previous_by_id) - current_ids
            for agent_id in sorted(removed_ids):
                yield {
                    "event": "agent_removed",
                    "data": json.dumps({"agent_id": agent_id}),
                }

            for approval in pending_approvals:
                approval_id = approval.approval_id
                if approval_id in seen_pending_approvals:
                    continue
                seen_pending_approvals.add(approval_id)
                yield {
                    "event": "permission_required",
                    "data": json.dumps(approval.to_dict()),
                }

            previous_by_id = {state.agent_id: state.signature() for state in states}

            if once:
                break
            await asyncio.sleep(interval)

    return EventSourceResponse(_event_generator())
