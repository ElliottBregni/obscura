"""Helpers for syncing session lifecycle and turns into vector memory."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from obscura.auth.models import AuthenticatedUser


def _safe_json(value: dict[str, Any]) -> str:
    """Serialize metadata for embedding text safely."""
    try:
        return json.dumps(value, sort_keys=True)
    except Exception:
        return "{}"


def sync_session_lifecycle(
    *,
    user: AuthenticatedUser,
    session_id: str,
    backend: str,
    event: str,
    details: dict[str, Any] | None = None,
) -> None:
    """Mirror a session lifecycle event into vector memory."""
    from obscura.vector_memory import VectorMemoryStore

    ts = datetime.now(UTC).isoformat()
    metadata: dict[str, Any] = {
        "session_id": session_id,
        "backend": backend,
        "event": event,
        "timestamp": ts,
        "details": details or {},
    }
    key = f"{session_id}:lifecycle"
    text = (
        f"Session lifecycle event={event} session_id={session_id} backend={backend} "
        f"timestamp={ts} details={_safe_json(details or {})}"
    )

    store = VectorMemoryStore.for_user(user)
    store.set(
        key,
        text,
        namespace="sessions",
        memory_type="session_event",
        metadata=metadata,
    )


def sync_session_turn(
    *,
    user: AuthenticatedUser,
    session_id: str,
    backend: str,
    prompt: str,
    response: str,
    mode: str,
) -> None:
    """Mirror a single session turn (prompt + response) into vector memory."""
    from obscura.vector_memory import VectorMemoryStore

    ts = datetime.now(UTC)
    ts_iso = ts.isoformat()
    key = f"{session_id}:turn:{int(ts.timestamp() * 1000)}"
    metadata: dict[str, Any] = {
        "session_id": session_id,
        "backend": backend,
        "mode": mode,
        "timestamp": ts_iso,
        "prompt_len": len(prompt),
        "response_len": len(response),
    }

    # Keep each embedding payload bounded while preserving turn semantics.
    prompt_snippet = prompt[:8000]
    response_snippet = response[:12000]
    text = (
        f"Session turn session_id={session_id} backend={backend} mode={mode} "
        f"timestamp={ts_iso}\nUser: {prompt_snippet}\nAssistant: {response_snippet}"
    )

    store = VectorMemoryStore.for_user(user)
    store.set(
        key,
        text,
        namespace="sessions",
        memory_type="session_turn",
        metadata=metadata,
    )
