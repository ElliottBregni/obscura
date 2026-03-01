"""System session ingestion from ~/.obscura into the unified event store."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from obscura.auth.models import AuthenticatedUser
from obscura.core.event_store import SQLiteEventStore, SessionStatus

_INDEX_FILE = Path.home() / ".obscura" / "agents" / "sessions" / "INDEX.jsonl"
_OBSCURA_HOME = Path.home() / ".obscura"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_AGENT_SYNC_SCRIPT = _PROJECT_ROOT / "scripts" / "agent_sync.py"


def preflight_system_session_ingest() -> dict[str, Any]:
    """Report filesystem/readiness checks for session ingest."""
    source_home = _OBSCURA_HOME
    sessions_root = source_home / "agents" / "sessions"
    script_exists = _AGENT_SYNC_SCRIPT.is_file()
    source_exists = source_home.exists()
    source_readable = os.access(source_home, os.R_OK) if source_exists else False
    sessions_writable = (
        os.access(sessions_root, os.W_OK)
        if sessions_root.exists()
        else os.access(sessions_root.parent, os.W_OK)
    )

    checks: dict[str, Any] = {
        "project_root": str(_PROJECT_ROOT),
        "agent_sync_script": str(_AGENT_SYNC_SCRIPT),
        "agent_sync_script_exists": script_exists,
        "obscura_home": str(source_home),
        "obscura_home_exists": source_exists,
        "obscura_home_readable": source_readable,
        "sessions_root": str(sessions_root),
        "sessions_root_writable": sessions_writable,
        "index_file": str(_INDEX_FILE),
        "index_exists": _INDEX_FILE.is_file(),
        "cwd": str(Path.cwd()),
    }
    checks["ready"] = bool(
        script_exists and source_exists and source_readable and sessions_writable
    )
    return checks


def copy_obscura_to_pwd(*, overwrite: bool = True) -> dict[str, Any]:
    """Copy ~/.obscura into current working directory as .obscura."""
    src = _OBSCURA_HOME
    dst = Path.cwd() / ".obscura"

    if not src.exists():
        raise RuntimeError(f"Source does not exist: {src}")
    if dst.exists() and not overwrite:
        raise RuntimeError(f"Destination already exists: {dst}")

    shutil.copytree(src, dst, dirs_exist_ok=overwrite)
    return {
        "source": str(src),
        "destination": str(dst),
        "overwrite": overwrite,
        "copied": True,
    }


def _load_index_entries(agent: str | None = None) -> list[dict[str, Any]]:
    if not _INDEX_FILE.is_file():
        return []

    entries: list[dict[str, Any]] = []
    for line in _INDEX_FILE.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if agent and payload.get("agent") != agent:
            continue
        entries.append(payload)
    return entries


def _ingest_entries(
    store: SQLiteEventStore,
    entries: list[dict[str, Any]],
    *,
    force: bool = False,
    user: AuthenticatedUser | None = None,
) -> tuple[int, int]:
    """Ingest INDEX.jsonl entries into the unified event store.

    Also indexes summaries/turns into VectorMemoryStore for semantic search.
    """
    ingested = 0
    skipped = 0

    for entry in entries:
        session_id = str(entry.get("id", "")).strip()
        agent = str(entry.get("agent", "")).strip()
        if not session_id or not agent:
            continue

        # Check if already ingested (sync call — this runs in a thread)
        existing = store._get_session_sync(session_id)
        if existing is not None and not force:
            skipped += 1
            continue

        # Build metadata from all extra INDEX fields
        metadata: dict[str, Any] = {}
        for key in (
            "turns", "cwds", "git_branch", "git_repo", "tools_used",
            "agent_version", "source_path", "synced_path",
        ):
            if key in entry:
                metadata[key] = entry[key]

        summary = str(entry.get("topic") or entry.get("summary") or "").strip()
        model = str(entry.get("model") or "").strip()
        project = str(entry.get("project") or "").strip()
        message_count = int(entry.get("message_count", 0))

        if existing is not None and force:
            # Update existing session
            store._update_session_sync(
                session_id,
                summary=summary,
                message_count=message_count,
                metadata=metadata,
            )
        else:
            # Create new session as 'ingested' + 'completed'
            store._create_session_sync(
                session_id,
                agent,
                backend=agent,
                model=model,
                source="ingested",
                project=project,
                summary=summary,
                metadata=metadata,
            )
            # Mark completed (ingested sessions aren't running)
            try:
                store._update_status_sync(session_id, SessionStatus.COMPLETED)
            except ValueError:
                pass

        # Index into VectorMemoryStore for semantic search
        if user is not None:
            _index_to_vector_memory(user, session_id, agent, entry, summary)

        ingested += 1

    return ingested, skipped


def _index_to_vector_memory(
    user: AuthenticatedUser,
    session_id: str,
    agent: str,
    entry: dict[str, Any],
    summary: str,
) -> None:
    """Index session summary and turns into VectorMemoryStore."""
    try:
        from obscura.vector_memory import VectorMemoryStore

        vector = VectorMemoryStore.for_user(user)

        if summary:
            vector.set(
                key=f"{session_id}:summary",
                text=summary,
                namespace="sessions",
                memory_type="summary",
                metadata={
                    "session_id": session_id,
                    "agent": agent,
                    "project": entry.get("project", ""),
                    "model": entry.get("model", ""),
                    "started": entry.get("started", ""),
                    "ended": entry.get("ended", ""),
                },
            )

        turns = entry.get("turns", [])
        if isinstance(turns, list) and turns:
            lines: list[str] = []
            for turn in turns:
                if not isinstance(turn, dict):
                    continue
                role = str(turn.get("role", "")).strip()
                preview = str(turn.get("preview", "")).strip()
                if role and preview:
                    lines.append(f"{role}: {preview}")

            if lines:
                vector.set(
                    key=f"{session_id}:turns",
                    text="\n".join(lines),
                    namespace="sessions",
                    memory_type="episode",
                    metadata={
                        "session_id": session_id,
                        "agent": agent,
                        "message_count": entry.get("message_count", 0),
                    },
                )
    except Exception:
        pass  # vector indexing is best-effort


def sync_and_ingest_system_sessions(
    user: AuthenticatedUser,
    *,
    agent: str | None = None,
    force: bool = False,
    copy_to_pwd: bool = False,
    copy_overwrite: bool = True,
    store: SQLiteEventStore | None = None,
) -> dict[str, Any]:
    """Run agent session sync from ~/.obscura, then ingest into unified event store."""
    copy_result: dict[str, Any] | None = None
    if copy_to_pwd:
        copy_result = copy_obscura_to_pwd(overwrite=copy_overwrite)

    cmd = [sys.executable, str(_AGENT_SYNC_SCRIPT), "--skip-memory"]
    if agent:
        cmd.extend(["--agent", agent])
    if force:
        cmd.append("--force")

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=300,
        cwd=_PROJECT_ROOT,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "agent_sync failed")

    # Use provided store or create one at the default location
    if store is None:
        from obscura.core.paths import resolve_obscura_home

        store = SQLiteEventStore(resolve_obscura_home() / "events.db")

    entries = _load_index_entries(agent=agent)
    ingested, skipped = _ingest_entries(
        store, entries, force=force, user=user,
    )

    return {
        "synced": True,
        "entries": len(entries),
        "ingested": ingested,
        "skipped": skipped,
        "agent": agent,
        "force": force,
        "index_path": str(_INDEX_FILE),
        "copy_to_pwd": copy_to_pwd,
        "copy_result": copy_result,
    }
