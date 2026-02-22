#!/usr/bin/env python3
"""Agent session sync — discover, copy, and index agent sessions.

Finds all sessions from ~/.claude, ~/.copilot, and ~/.codex, copies them
into ~/obscura/agents/sessions/, and produces a unified semantic index
(INDEX.jsonl) that normalizes sessions across all three agents.

The semantic layer makes sessions ingestible regardless of source agent:
each INDEX.jsonl line contains id, agent, project, model, timestamps,
summary, message count, turn previews, and file manifest.

Complements scripts/sync.py which handles vault-to-repo config sync.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Add project root to path so we can import obscura modules
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from obscura.auth.models import AuthenticatedUser  # noqa: E402
from obscura.memory import MemoryStore  # noqa: E402
from obscura.vector_memory.vector_memory import VectorMemoryStore  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Agent sessions are always user-global (not per-project), so use $HOME
_OBSCURA_HOME = Path(
    os.environ.get("OBSCURA_HOME", "").strip() or str(Path.home() / ".obscura")
)
SESSIONS_DIR = _OBSCURA_HOME / "agents" / "sessions"
INDEX_FILE = SESSIONS_DIR / "INDEX.jsonl"
TURN_PREVIEW_LENGTH = 120


@dataclass
class AgentSource:
    """Configuration for one agent's session source."""

    name: str
    source_dir: Path
    exclude_dirs: set[str] = field(default_factory=lambda: set[str]())


AGENT_SOURCES: dict[str, AgentSource] = {
    "claude": AgentSource(
        name="claude",
        source_dir=Path.home() / ".claude",
        exclude_dirs=set(),
    ),
    "copilot": AgentSource(
        name="copilot",
        source_dir=Path.home() / ".copilot",
        exclude_dirs={"logs", "ide", "pkg"},
    ),
    "codex": AgentSource(
        name="codex",
        source_dir=Path.home() / ".codex",
        exclude_dirs={
            "log",
            "sqlite",
            "tmp",
            "vendor_imports",
            "shell_snapshots",
            "rules",
            "skills",
        },
    ),
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DiscoveredSession:
    """A discovered session on disk, before parsing."""

    agent: str
    session_id: str
    source_path: Path
    files: list[tuple[Path, Path]]  # (source_abs, dest_relative)
    mtime: float = 0.0
    meta: dict[str, Any] = field(default_factory=lambda: dict[str, Any]())


@dataclass
class SessionEntry:
    """One entry in INDEX.jsonl — the semantic layer."""

    id: str
    agent: str
    project: str
    model: str
    started: str
    ended: str
    summary: str
    message_count: int
    source_path: str
    synced_path: str
    files: list[str]
    turns: list[dict[str, str]]
    # Enriched context fields
    slug: str = ""
    cwds: list[str] = field(default_factory=lambda: list[str]())
    git_branch: str = ""
    git_repo: str = ""
    tools_used: list[str] = field(default_factory=lambda: list[str]())
    agent_version: str = ""
    source: str = ""  # cli, vscode, etc.
    topic: str = ""  # Compact topic/subject label

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "agent": self.agent,
            "project": self.project,
            "model": self.model,
            "started": self.started,
            "ended": self.ended,
            "summary": self.summary,
            "message_count": self.message_count,
            "source_path": self.source_path,
            "synced_path": self.synced_path,
            "files": self.files,
            "turns": self.turns,
        }
        # Only include enriched fields when populated (keeps index compact)
        if self.slug:
            d["slug"] = self.slug
        if self.cwds:
            d["cwds"] = self.cwds
        if self.git_branch:
            d["git_branch"] = self.git_branch
        if self.git_repo:
            d["git_repo"] = self.git_repo
        if self.tools_used:
            d["tools_used"] = self.tools_used
        if self.agent_version:
            d["agent_version"] = self.agent_version
        if self.source:
            d["source"] = self.source
        if self.topic:
            d["topic"] = self.topic
        return d


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ms_to_iso(ms: int) -> str:
    """Convert millisecond timestamp to ISO 8601."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _unix_to_iso(ts: int | float) -> str:
    """Convert unix seconds to ISO 8601."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _truncate(text: str, length: int = TURN_PREVIEW_LENGTH) -> str:
    """Truncate text to length, adding ellipsis if needed."""
    text = text.replace("\n", " ").strip()
    if len(text) <= length:
        return text
    return text[: length - 3] + "..."


def _parse_simple_yaml(path: Path) -> dict[str, str]:
    """Parse simple YAML (flat key: value with optional |- multiline blocks).

    Handles the workspace.yaml format used by Copilot:
        id: uuid
        cwd: /path
        summary: |-
          multiline text
          continues here
    """
    if not path.is_file():
        return {}

    result: dict[str, str] = {}
    current_key: str | None = None
    multiline_lines: list[str] = []
    in_multiline = False
    indent_level = 0

    for line in path.read_text(errors="replace").splitlines():
        if in_multiline:
            stripped = line.lstrip()
            line_indent = len(line) - len(stripped)
            if line_indent > indent_level or (stripped == "" and current_key):
                multiline_lines.append(stripped)
                continue
            else:
                # End of multiline block
                if current_key:
                    result[current_key] = "\n".join(multiline_lines).strip()
                in_multiline = False
                current_key = None
                multiline_lines = []

        if ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value == "|-" or value == "|":
                current_key = key
                in_multiline = True
                indent_level = 0
                multiline_lines = []
            else:
                result[key] = value

    # Flush any trailing multiline
    if in_multiline and current_key:
        result[current_key] = "\n".join(multiline_lines).strip()

    return result


def _safe_read_jsonl(path: Path, limit: int = 0) -> list[dict[str, Any]]:
    """Read a JSONL file, skipping malformed lines."""
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if limit and len(entries) >= limit:
                break
    except OSError:
        pass
    return entries


def _derive_topic(
    summary: str,
    turns: list[dict[str, str]],
    project: str = "",
    tools: list[str] | None = None,
) -> str:
    """Derive a compact topic label from session context.

    Produces a short (3-10 word) topic that captures what the session was about.
    Uses the first user message, summary, project name, and tool usage as signals.
    """
    # Start with the first user turn as the primary signal
    first_user = ""
    for t in turns:
        if t.get("role") == "user":
            first_user = t.get("preview", "")
            break

    text = summary or first_user
    if not text:
        if project:
            return Path(project).name
        return ""

    # Strip quotes wrapping the whole text
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1]

    # If text is a URL, extract the meaningful part
    if text.startswith(("http://", "https://")):
        # Use path segments as topic
        parts = text.split("/")
        meaningful = [
            p for p in parts[3:] if p and p not in ("tree", "dev", "main", "master")
        ]
        if meaningful:
            text = " ".join(meaningful[-2:])
        elif len(parts) > 2:
            text = parts[2]  # domain
        if project:
            text = f"{Path(project).name}: {text}"

    # Strip common conversational prefixes
    for prefix in (
        "I want you to ",
        "I want to ",
        "I need you to ",
        "I need to ",
        "Can you ",
        "Could you ",
        "Will you ",
        "Would you ",
        "Help me ",
        "Please ",
        "Generate ",
        "Create ",
        "Write ",
    ):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix) :]
            # Capitalize first char
            if text:
                text = text[0].upper() + text[1:]
            break

    # Truncate to first sentence or clause
    for sep in (". ", "! ", "? ", "\n", " — ", " - ", "; "):
        idx = text.find(sep)
        if 0 < idx < 80:
            text = text[:idx]
            break

    # Remove trailing ellipsis from truncated previews
    if text.endswith("..."):
        text = text[:-3].rsplit(" ", 1)[0]

    # Cap at ~50 chars for compactness
    text = text.strip()
    if len(text) > 50:
        text = text[:47].rsplit(" ", 1)[0] + "..."

    return text


# ---------------------------------------------------------------------------
# SessionDiscovery — find sessions from agent source dirs
# ---------------------------------------------------------------------------


class SessionDiscovery:
    """Discovers sessions from agent source directories."""

    def discover(self, source: AgentSource) -> list[DiscoveredSession]:
        if not source.source_dir.is_dir():
            return []
        dispatch = {
            "claude": self._discover_claude,
            "copilot": self._discover_copilot,
            "codex": self._discover_codex,
        }
        fn = dispatch.get(source.name)
        if fn is None:
            return []
        return fn(source)

    # -- Claude -------------------------------------------------------------

    def _discover_claude(self, source: AgentSource) -> list[DiscoveredSession]:
        """Discover Claude sessions by parsing history.jsonl and scanning projects/."""
        base = source.source_dir
        history_path = base / "history.jsonl"

        # Build session -> metadata from history
        session_meta: dict[str, dict[str, Any]] = {}
        for entry in _safe_read_jsonl(history_path):
            sid = entry.get("sessionId", "")
            if not sid:
                continue
            if sid not in session_meta:
                session_meta[sid] = {
                    "project": entry.get("project", ""),
                    "first_display": entry.get("display", ""),
                    "timestamps": [],
                }
            session_meta[sid]["timestamps"].append(entry.get("timestamp", 0))

        # Also scan projects/ dirs for session files not in history
        projects_dir = base / "projects"
        if projects_dir.is_dir():
            for project_dir in sorted(projects_dir.iterdir()):
                if not project_dir.is_dir():
                    continue
                for f in sorted(project_dir.iterdir()):
                    if f.suffix == ".jsonl" and f.stem not in session_meta:
                        session_meta[f.stem] = {
                            "project": "",
                            "first_display": "",
                            "timestamps": [],
                        }

        sessions: list[DiscoveredSession] = []
        for sid, meta in session_meta.items():
            files: list[tuple[Path, Path]] = []
            max_mtime: float = 0.0

            # Conversation logs from projects/
            if projects_dir.is_dir():
                for project_dir in sorted(projects_dir.iterdir()):
                    if not project_dir.is_dir():
                        continue
                    conv_file = project_dir / f"{sid}.jsonl"
                    if conv_file.is_file():
                        files.append((conv_file, Path("conversation.jsonl")))
                        max_mtime = max(max_mtime, conv_file.stat().st_mtime)
                    # Also check for session subdirectory
                    session_subdir = project_dir / sid
                    if session_subdir.is_dir():
                        for sub_file in session_subdir.rglob("*"):
                            if sub_file.is_file():
                                rel = sub_file.relative_to(session_subdir)
                                files.append((sub_file, Path("data") / rel))
                                max_mtime = max(max_mtime, sub_file.stat().st_mtime)

            # Debug log
            debug_file = base / "debug" / f"{sid}.txt"
            if debug_file.is_file():
                files.append((debug_file, Path("debug.txt")))
                max_mtime = max(max_mtime, debug_file.stat().st_mtime)

            # Todos
            todos_dir = base / "todos"
            if todos_dir.is_dir():
                for todo_file in sorted(todos_dir.iterdir()):
                    if todo_file.is_file() and sid in todo_file.name:
                        files.append((todo_file, Path("todos.json")))
                        max_mtime = max(max_mtime, todo_file.stat().st_mtime)
                        break

            # Session env
            env_dir = base / "session-env" / sid
            if env_dir.is_dir():
                for env_file in sorted(env_dir.rglob("*")):
                    if env_file.is_file():
                        rel = env_file.relative_to(env_dir)
                        files.append((env_file, Path("env") / rel))
                        max_mtime = max(max_mtime, env_file.stat().st_mtime)

            if not files:
                continue

            sessions.append(
                DiscoveredSession(
                    agent="claude",
                    session_id=sid,
                    source_path=base,
                    files=files,
                    mtime=max_mtime,
                    meta=meta,
                )
            )

        return sessions

    # -- Copilot ------------------------------------------------------------

    def _discover_copilot(self, source: AgentSource) -> list[DiscoveredSession]:
        """Discover Copilot sessions from session-state/ directory."""
        session_state = source.source_dir / "session-state"
        if not session_state.is_dir():
            return []

        sessions: list[DiscoveredSession] = []
        try:
            entries = list(os.scandir(session_state))
        except OSError:
            return []

        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue

            sid = entry.name
            session_dir = Path(entry.path)
            files: list[tuple[Path, Path]] = []
            max_mtime: float = 0.0

            try:
                dir_mtime = entry.stat().st_mtime
                max_mtime = dir_mtime
            except OSError:
                continue

            # Collect session files
            for child in sorted(session_dir.iterdir()):
                if child.name in source.exclude_dirs:
                    continue
                if child.is_file():
                    files.append((child, Path(child.name)))
                    try:
                        max_mtime = max(max_mtime, child.stat().st_mtime)
                    except OSError:
                        pass
                elif child.is_dir():
                    for sub in sorted(child.rglob("*")):
                        if sub.is_file():
                            rel = sub.relative_to(session_dir)
                            files.append((sub, rel))

            if not files:
                continue

            sessions.append(
                DiscoveredSession(
                    agent="copilot",
                    session_id=sid,
                    source_path=session_dir,
                    files=files,
                    mtime=max_mtime,
                )
            )

        return sessions

    # -- Codex --------------------------------------------------------------

    def _discover_codex(self, source: AgentSource) -> list[DiscoveredSession]:
        """Discover Codex sessions from sessions/ directory tree."""
        base = source.source_dir
        sessions_dir = base / "sessions"
        if not sessions_dir.is_dir():
            return []

        # Parse history and global state for enrichment
        history_entries: dict[str, list[dict[str, Any]]] = {}
        for entry in _safe_read_jsonl(base / "history.jsonl"):
            sid = entry.get("session_id", "")
            if sid:
                history_entries.setdefault(sid, []).append(entry)

        thread_titles: dict[str, str] = {}
        global_state_path = base / ".codex-global-state.json"
        if global_state_path.is_file():
            try:
                state = json.loads(global_state_path.read_text(errors="replace"))
                persisted = state.get("electron-persisted-atom-state", {})
                titles_data = persisted.get("thread-titles", {})
                thread_titles = titles_data.get("titles", {})
            except (json.JSONDecodeError, OSError):
                pass

        sessions: list[DiscoveredSession] = []
        # Pattern: rollout-{timestamp}-{session-id}.jsonl
        rollout_pattern = re.compile(
            r"^rollout-[\dT-]+-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
        )

        for rollout_file in sorted(sessions_dir.rglob("rollout-*.jsonl")):
            m = rollout_pattern.match(rollout_file.name)
            if not m:
                continue

            sid = m.group(1)
            try:
                mtime = rollout_file.stat().st_mtime
            except OSError:
                continue

            files: list[tuple[Path, Path]] = [
                (rollout_file, Path("rollout.jsonl")),
            ]

            meta: dict[str, Any] = {
                "history": history_entries.get(sid, []),
                "title": thread_titles.get(sid, ""),
            }

            sessions.append(
                DiscoveredSession(
                    agent="codex",
                    session_id=sid,
                    source_path=rollout_file.parent,
                    files=files,
                    mtime=mtime,
                    meta=meta,
                )
            )

        return sessions


# ---------------------------------------------------------------------------
# SessionCopier — incremental file sync
# ---------------------------------------------------------------------------


class SessionCopier:
    """Copy session files to the sync destination with mtime-based skipping."""

    def __init__(self, dest_base: Path, dry_run: bool = False) -> None:
        self.dest_base = dest_base
        self.dry_run = dry_run

    def sync_session(
        self,
        session: DiscoveredSession,
        force: bool = False,
    ) -> tuple[int, int]:
        """Copy session files. Returns (copied, skipped)."""
        dest_dir = self.dest_base / session.agent / session.session_id
        copied = 0
        skipped = 0

        for source_abs, dest_rel in session.files:
            dest_file = dest_dir / dest_rel

            if not force and dest_file.is_file():
                try:
                    if dest_file.stat().st_mtime >= source_abs.stat().st_mtime:
                        skipped += 1
                        continue
                except OSError:
                    pass

            if not self.dry_run:
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_abs, dest_file)
            copied += 1

        # Write meta.json enrichment file (only if new or content changed)
        if session.meta:
            meta_path = dest_dir / "meta.json"
            meta_content = json.dumps(session.meta, indent=2, default=str)
            write_meta = True
            if not force and meta_path.is_file():
                try:
                    if meta_path.read_text() == meta_content:
                        write_meta = False
                except OSError:
                    pass
            if write_meta and not self.dry_run:
                dest_dir.mkdir(parents=True, exist_ok=True)
                meta_path.write_text(meta_content)

        return copied, skipped

    def get_last_sync(self, agent: str) -> float:
        """Get the last sync timestamp for an agent."""
        marker = self.dest_base / agent / ".last-sync"
        if marker.is_file():
            try:
                return float(marker.read_text().strip())
            except (ValueError, OSError):
                pass
        return 0.0

    def set_last_sync(self, agent: str) -> None:
        """Record current time as last sync for an agent."""
        if self.dry_run:
            return
        marker_dir = self.dest_base / agent
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / ".last-sync").write_text(str(time.time()))


# ---------------------------------------------------------------------------
# SemanticIndexBuilder — parse synced copies, build INDEX.jsonl
# ---------------------------------------------------------------------------


class SemanticIndexBuilder:
    """Parse synced session copies and produce a unified INDEX.jsonl."""

    def __init__(self, sessions_dir: Path) -> None:
        self.sessions_dir = sessions_dir

    def build(self, agents: list[str] | None = None) -> list[SessionEntry]:
        """Parse all synced sessions and return index entries."""
        target_agents = agents or list(AGENT_SOURCES.keys())
        entries: list[SessionEntry] = []

        for agent in target_agents:
            agent_dir = self.sessions_dir / agent
            if not agent_dir.is_dir():
                continue

            dispatch = {
                "claude": self._parse_claude,
                "copilot": self._parse_copilot,
                "codex": self._parse_codex,
            }
            parser = dispatch.get(agent)
            if parser is None:
                continue

            for session_dir in sorted(agent_dir.iterdir()):
                if not session_dir.is_dir() or session_dir.name.startswith("."):
                    continue
                try:
                    entry = parser(session_dir)
                    if entry:
                        entries.append(entry)
                except Exception as e:
                    print(
                        f"  Warning: failed to parse {session_dir.name}: {e}",
                        file=sys.stderr,
                    )

        # Sort by start time descending (most recent first)
        entries.sort(key=lambda e: e.started or "", reverse=True)
        return entries

    def write_index(self, entries: list[SessionEntry]) -> None:
        """Write INDEX.jsonl atomically."""
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = INDEX_FILE.with_suffix(".jsonl.tmp")
        with tmp_path.open("w") as f:
            for entry in entries:
                f.write(json.dumps(entry.to_dict(), separators=(",", ":")) + "\n")
        os.replace(tmp_path, INDEX_FILE)

    # -- Claude parser ------------------------------------------------------

    def _parse_claude(self, session_dir: Path) -> SessionEntry | None:
        sid = session_dir.name
        conv_path = session_dir / "conversation.jsonl"
        meta_path = session_dir / "meta.json"

        meta: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(errors="replace"))
            except (json.JSONDecodeError, OSError):
                pass

        project = meta.get("project", "")
        model = ""
        turns: list[dict[str, str]] = []
        timestamps: list[str] = []
        # Context extraction
        cwds: set[str] = set()
        branches: set[str] = set()
        tools_used: set[str] = set()
        slug = ""
        agent_version = ""

        # Parse conversation log
        for event in _safe_read_jsonl(conv_path):
            event_type = event.get("type", "")
            msg = event.get("message", {})
            role = msg.get("role", "")
            ts = event.get("timestamp", "")

            # Extract context from every event
            if event.get("cwd"):
                cwds.add(event["cwd"])
            if event.get("gitBranch") and event["gitBranch"] != "HEAD":
                branches.add(event["gitBranch"])
            if event.get("slug"):
                slug = event["slug"]
            if event.get("version") and not agent_version:
                agent_version = event["version"]

            if event_type == "user" and role == "user":
                content: Any = msg.get("content", "")
                if isinstance(content, str):
                    preview = _truncate(content)
                elif isinstance(content, list):
                    texts: list[str] = []
                    content_list: list[dict[str, Any] | str] = content  # type: ignore[assignment]
                    for block in content_list:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                texts.append(str(block.get("text", "")))
                            elif block.get("type") == "tool_result":
                                continue
                        else:
                            texts.append(str(block))
                    preview = _truncate(" ".join(texts))
                else:
                    preview = ""

                if preview:
                    turns.append({"ts": ts, "role": "user", "preview": preview})
                if ts:
                    timestamps.append(ts)

            elif event_type == "assistant" and role == "assistant":
                if not model:
                    model = msg.get("model", "")

                asst_content: Any = msg.get("content", [])
                texts_a: list[str] = []
                if isinstance(asst_content, list):
                    asst_list: list[dict[str, Any] | str] = asst_content  # type: ignore[assignment]
                    for ablock in asst_list:
                        if isinstance(ablock, dict):
                            if ablock.get("type") == "text":
                                texts_a.append(str(ablock.get("text", "")))
                            elif ablock.get("type") == "tool_use":
                                tools_used.add(str(ablock.get("name", "")))
                elif isinstance(asst_content, str):
                    texts_a.append(asst_content)

                text = " ".join(texts_a).strip()
                if text:
                    preview = _truncate(text)
                    turns.append({"ts": ts, "role": "assistant", "preview": preview})
                if ts:
                    timestamps.append(ts)

        # If no conversation log, try to use meta timestamps
        if not timestamps and meta.get("timestamps"):
            timestamps = [_ms_to_iso(t) for t in meta["timestamps"]]

        # Fill project from cwd if history didn't have it
        if not project and cwds:
            # Use the most specific (longest) cwd as project
            project = max(cwds, key=len)

        # Derive summary
        summary = meta.get("first_display", "")
        if not summary and turns:
            summary = turns[0].get("preview", "")
        summary = _truncate(summary, 200)

        started = min(timestamps) if timestamps else ""
        ended = max(timestamps) if timestamps else ""

        files = [
            str(f.relative_to(session_dir))
            for f in sorted(session_dir.rglob("*"))
            if f.is_file() and f.name != "meta.json"
        ]

        sorted_tools = sorted(tools_used)
        topic = _derive_topic(summary, turns, project, sorted_tools)

        return SessionEntry(
            id=sid,
            agent="claude",
            project=project,
            model=model or "claude",
            started=started,
            ended=ended,
            summary=summary,
            message_count=len([t for t in turns if t["role"] == "user"]),
            source_path="~/.claude/",
            synced_path=f"claude/{sid}/",
            files=files,
            turns=turns,
            slug=slug,
            cwds=sorted(cwds),
            git_branch=sorted(branches)[0] if branches else "",
            tools_used=sorted_tools,
            agent_version=agent_version,
            source="claude-code",
            topic=topic,
        )

    # -- Copilot parser -----------------------------------------------------

    def _parse_copilot(self, session_dir: Path) -> SessionEntry | None:
        sid = session_dir.name

        # Parse workspace.yaml
        ws = _parse_simple_yaml(session_dir / "workspace.yaml")
        project = ws.get("cwd", "")
        started = ws.get("created_at", "")
        ended = ws.get("updated_at", "")
        summary = ws.get("summary", "")

        # Parse events.jsonl for model, turns, and context
        model = ""
        agent_version = ""
        turns: list[dict[str, str]] = []
        tools_used: set[str] = set()
        user_count = 0

        for event in _safe_read_jsonl(session_dir / "events.jsonl"):
            event_type = event.get("type", "")
            data = event.get("data", {})
            ts = event.get("timestamp", "")

            if event_type == "session.start":
                if not model:
                    model = data.get("selectedModel", "")
                if not agent_version:
                    agent_version = data.get("copilotVersion", "")
                if not project:
                    ctx = data.get("context", {})
                    project = ctx.get("cwd", "")
                if not started:
                    started = data.get("startTime", ts)

            elif event_type == "session.model_change":
                model = data.get("newModel", model)

            elif event_type == "session.info":
                # Model change info events
                msg = data.get("message", "")
                if msg.startswith("Model changed to: "):
                    model = msg[len("Model changed to: ") :]

            elif event_type == "user.message":
                content = data.get("content", "")
                if content:
                    turns.append(
                        {
                            "ts": ts,
                            "role": "user",
                            "preview": _truncate(content),
                        }
                    )
                    user_count += 1
                    if not summary:
                        summary = _truncate(content, 200)

            elif event_type == "assistant.message":
                content = data.get("content", "")
                if content:
                    turns.append(
                        {
                            "ts": ts,
                            "role": "assistant",
                            "preview": _truncate(content),
                        }
                    )

            elif event_type == "tool.execution_start":
                tool_name = data.get("toolName", "")
                if tool_name:
                    tools_used.add(tool_name)

        summary = _truncate(summary, 200)
        sorted_tools = sorted(tools_used)
        topic = _derive_topic(summary, turns, project, sorted_tools)

        # Collect file list
        files = [
            str(f.relative_to(session_dir))
            for f in sorted(session_dir.rglob("*"))
            if f.is_file()
        ]

        return SessionEntry(
            id=sid,
            agent="copilot",
            project=project,
            model=model,
            started=started,
            ended=ended,
            summary=summary,
            message_count=user_count,
            source_path=f"~/.copilot/session-state/{sid}/",
            synced_path=f"copilot/{sid}/",
            files=files,
            turns=turns,
            cwds=[project] if project else [],
            tools_used=sorted_tools,
            agent_version=agent_version,
            source="copilot-agent",
            topic=topic,
        )

    # -- Codex parser -------------------------------------------------------

    def _parse_codex(self, session_dir: Path) -> SessionEntry | None:
        sid = session_dir.name
        rollout = session_dir / "rollout.jsonl"
        meta_path = session_dir / "meta.json"

        # Load enrichment meta
        meta: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(errors="replace"))
            except (json.JSONDecodeError, OSError):
                pass

        project = ""
        model = ""
        source = ""
        agent_version = ""
        git_branch = ""
        git_repo = ""
        turns: list[dict[str, str]] = []
        timestamps: list[str] = []
        tools_used: set[str] = set()

        for event in _safe_read_jsonl(rollout):
            event_type = event.get("type", "")
            payload = event.get("payload", {})
            ts = event.get("timestamp", "")

            if event_type == "session_meta":
                project = payload.get("cwd", "")
                if not model:
                    model = payload.get("model_provider", "")
                source = payload.get("source", "")  # cli, vscode
                agent_version = payload.get("cli_version", "")
                # Git info
                git_info = payload.get("git", {})
                if git_info:
                    git_branch = git_info.get("branch", "")
                    git_repo = git_info.get("repository_url", "")

            elif event_type == "turn_context":
                m = payload.get("model", "")
                if m:
                    model = m  # Use actual model, overrides model_provider

            elif event_type == "response_item":
                role = str(payload.get("role", ""))
                content_blocks: list[dict[str, Any]] = payload.get("content", [])
                item_type = str(payload.get("type", ""))

                # Skip developer/system messages entirely
                if role == "developer":
                    continue

                # Track tool usage
                if item_type in ("function_call", "custom_tool_call"):
                    tool_name = payload.get("name", "")
                    if tool_name:
                        tools_used.add(tool_name)
                    if ts:
                        timestamps.append(ts)
                    continue

                if item_type in (
                    "function_call_output",
                    "custom_tool_call_output",
                    "reasoning",
                    "ghost_snapshot",
                ):
                    if ts:
                        timestamps.append(ts)
                    continue

                if item_type == "web_search_call":
                    tools_used.add("web_search")
                    if ts:
                        timestamps.append(ts)
                    continue

                if role == "user":
                    u_texts: list[str] = []
                    for cb in content_blocks:
                        if cb.get("type") == "input_text":
                            text_val = str(cb.get("text", ""))
                            # Skip system context blocks (instructions, env, permissions)
                            if text_val.startswith(("<", "# AGENTS.md")):
                                continue
                            u_texts.append(text_val)
                    text = " ".join(u_texts).strip()
                    if text:
                        turns.append(
                            {
                                "ts": ts,
                                "role": "user",
                                "preview": _truncate(text),
                            }
                        )
                        if ts:
                            timestamps.append(ts)

                elif role == "assistant":
                    a_texts: list[str] = []
                    for cb in content_blocks:
                        t = str(cb.get("type", ""))
                        if t in ("output_text", "input_text", "text"):
                            a_texts.append(str(cb.get("text", "")))
                    text = " ".join(a_texts).strip()
                    if text:
                        turns.append(
                            {
                                "ts": ts,
                                "role": "assistant",
                                "preview": _truncate(text),
                            }
                        )
                        if ts:
                            timestamps.append(ts)

            elif event_type == "event_msg":
                if ts:
                    timestamps.append(ts)

        # Enrich from meta — prefer thread title, then first user message from history
        summary = meta.get("title", "")
        if not summary:
            history = meta.get("history", [])
            if history:
                summary = history[0].get("text", "")
        if not summary:
            user_turns = [t for t in turns if t["role"] == "user"]
            if user_turns:
                summary = user_turns[0].get("preview", "")
        summary = _truncate(summary, 200)

        started = min(timestamps) if timestamps else ""
        ended = max(timestamps) if timestamps else ""

        files = [
            str(f.relative_to(session_dir))
            for f in sorted(session_dir.rglob("*"))
            if f.is_file() and f.name != "meta.json"
        ]

        sorted_tools = sorted(tools_used)
        topic = _derive_topic(summary, turns, project, sorted_tools)

        return SessionEntry(
            id=sid,
            agent="codex",
            project=project,
            model=model,
            started=started,
            ended=ended,
            summary=summary,
            message_count=len([t for t in turns if t["role"] == "user"]),
            source_path="~/.codex/sessions/",
            synced_path=f"codex/{sid}/",
            files=files,
            turns=turns,
            cwds=[project] if project else [],
            git_branch=git_branch,
            git_repo=git_repo,
            tools_used=sorted_tools,
            agent_version=agent_version,
            source=source or "codex",
            topic=topic,
        )


# ---------------------------------------------------------------------------
# Local user factory
# ---------------------------------------------------------------------------


def _local_user() -> AuthenticatedUser:
    """Create an AuthenticatedUser from the machine hostname."""
    hostname = socket.gethostname()
    return AuthenticatedUser(
        user_id=f"local-{hostname}",
        email=f"{hostname}@local",
        roles=("admin",),
        org_id="local",
        token_type="local",
        raw_token="",
    )


# ---------------------------------------------------------------------------
# SessionMemoryIngestor — ingest sessions into MemoryStore + VectorMemoryStore
# ---------------------------------------------------------------------------


class SessionMemoryIngestor:
    """Ingest SessionEntry objects into .obscura memory stores."""

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self._user = _local_user()
        self._memory: MemoryStore | None = None
        self._vector: VectorMemoryStore | None = None

    @property
    def memory(self) -> MemoryStore:
        if self._memory is None:
            self._memory = MemoryStore(self._user)
        return self._memory

    @property
    def vector(self) -> VectorMemoryStore:
        if self._vector is None:
            self._vector = VectorMemoryStore(self._user)
        return self._vector

    def ingest(
        self, entries: list[SessionEntry], force: bool = False
    ) -> tuple[int, int]:
        """Ingest session entries into memory stores.

        Returns (ingested, skipped).
        """
        ingested = 0
        skipped = 0

        for entry in entries:
            if not force and self._already_ingested(entry.id):
                skipped += 1
                continue

            if not self.dry_run:
                self._ingest_kv(entry)
                self._ingest_vector(entry)
            ingested += 1

        if not self.dry_run and (self._memory or self._vector):
            if self._memory:
                self._memory.close()
            if self._vector:
                self._vector.close()

        return ingested, skipped

    def _already_ingested(self, session_id: str) -> bool:
        """Check if a session is already in the key-value store."""
        return self.memory.get(session_id, namespace="sessions") is not None

    def _ingest_kv(self, entry: SessionEntry) -> None:
        """Write session metadata to MemoryStore."""
        self.memory.set(
            key=entry.id,
            value=entry.to_dict(),
            namespace="sessions",
        )

    def _ingest_vector(self, entry: SessionEntry) -> None:
        """Write session content to VectorMemoryStore for semantic search."""
        # Summary embedding
        summary_text = entry.topic or entry.summary
        if summary_text:
            metadata: dict[str, Any] = {
                "session_id": entry.id,
                "agent": entry.agent,
                "project": entry.project,
                "model": entry.model,
                "started": entry.started,
                "ended": entry.ended,
            }
            if entry.tools_used:
                metadata["tools_used"] = entry.tools_used
            if entry.git_branch:
                metadata["git_branch"] = entry.git_branch
            if entry.git_repo:
                metadata["git_repo"] = entry.git_repo

            self.vector.set(
                key=f"{entry.id}:summary",
                text=summary_text,
                metadata=metadata,
                namespace="sessions",
                memory_type="summary",
            )

        # Turn previews embedding
        if entry.turns:
            conversation = "\n".join(
                f"{t['role']}: {t['preview']}" for t in entry.turns
            )
            self.vector.set(
                key=f"{entry.id}:turns",
                text=conversation,
                metadata={
                    "session_id": entry.id,
                    "agent": entry.agent,
                    "message_count": entry.message_count,
                },
                namespace="sessions",
                memory_type="episode",
            )


# ---------------------------------------------------------------------------
# SessionCleaner — remove synced data
# ---------------------------------------------------------------------------


class SessionCleaner:
    """Remove synced session data."""

    def __init__(self, sessions_dir: Path, dry_run: bool = False) -> None:
        self.sessions_dir = sessions_dir
        self.dry_run = dry_run

    def clean(self, agent: str | None = None) -> None:
        """Remove synced session directories and INDEX."""
        agents = [agent] if agent else list(AGENT_SOURCES.keys())
        for a in agents:
            agent_dir = self.sessions_dir / a
            if agent_dir.is_dir():
                count = sum(1 for d in agent_dir.iterdir() if d.is_dir())
                print(f"  [{a}] Removing {count} synced sessions")
                if not self.dry_run:
                    shutil.rmtree(agent_dir)

        # Remove INDEX.jsonl only when cleaning all agents
        if agent is None and INDEX_FILE.is_file():
            print("  Removing INDEX.jsonl")
            if not self.dry_run:
                INDEX_FILE.unlink()


# ---------------------------------------------------------------------------
# AgentSessionSync — orchestrator
# ---------------------------------------------------------------------------


class AgentSessionSync:
    """Orchestrator: coordinates discovery, copy, indexing, and cleanup."""

    def __init__(
        self,
        sessions_dir: Path = SESSIONS_DIR,
        dry_run: bool = False,
    ) -> None:
        self.sessions_dir = sessions_dir
        self.dry_run = dry_run
        self._discovery = SessionDiscovery()
        self._copier = SessionCopier(sessions_dir, dry_run=dry_run)
        self._indexer = SemanticIndexBuilder(sessions_dir)
        self._cleaner = SessionCleaner(sessions_dir, dry_run=dry_run)
        self._ingestor = SessionMemoryIngestor(dry_run=dry_run)

    def sync_all(
        self,
        agent: str | None = None,
        force: bool = False,
        skip_memory: bool = False,
    ) -> None:
        """Discover, copy, and index all sessions."""
        agents = [agent] if agent else list(AGENT_SOURCES.keys())
        total_copied = 0
        total_skipped = 0
        total_sessions = 0

        for agent_name in agents:
            source = AGENT_SOURCES.get(agent_name)
            if source is None:
                print(f"  Unknown agent: {agent_name}", file=sys.stderr)
                continue

            if not source.source_dir.is_dir():
                print(f"  [{agent_name}] Source not found: {source.source_dir}")
                continue

            print(f"\nDiscovering {agent_name} sessions...")
            last_sync = self._copier.get_last_sync(agent_name)
            all_sessions = self._discovery.discover(source)

            # Filter by last sync time for efficiency (unless force)
            if not force and last_sync > 0:
                sessions = [s for s in all_sessions if s.mtime > last_sync]
                skipped_unchanged = len(all_sessions) - len(sessions)
                if skipped_unchanged > 0:
                    print(
                        f"  [{agent_name}] Skipping {skipped_unchanged} unchanged sessions (last sync: {_unix_to_iso(last_sync)[:19]})"
                    )
            else:
                sessions = all_sessions

            agent_copied = 0
            agent_skipped = 0

            for session in sessions:
                copied, skipped = self._copier.sync_session(session, force=force)
                agent_copied += copied
                agent_skipped += skipped

            total_copied += agent_copied
            total_skipped += agent_skipped
            total_sessions += len(sessions)

            self._copier.set_last_sync(agent_name)
            print(
                f"  [{agent_name}] {len(sessions)} sessions processed: "
                f"{agent_copied} files copied, {agent_skipped} unchanged"
            )

        # Build semantic index from all synced copies
        print("\nBuilding semantic index...")
        entries = self._indexer.build()
        if not self.dry_run:
            self._indexer.write_index(entries)
        print(
            f"  INDEX.jsonl: {len(entries)} sessions indexed "
            f"({sum(1 for e in entries if e.agent == 'claude')} claude, "
            f"{sum(1 for e in entries if e.agent == 'copilot')} copilot, "
            f"{sum(1 for e in entries if e.agent == 'codex')} codex)"
        )

        # Ingest into .obscura memory stores
        if not skip_memory:
            self._ingest_to_memory(entries, force=force)

        print(
            f"\nSync complete. {total_copied} files copied, {total_skipped} unchanged."
        )

    def _ingest_to_memory(
        self, entries: list[SessionEntry], force: bool = False
    ) -> None:
        """Ingest session entries into MemoryStore + VectorMemoryStore."""
        print("\nIngesting into .obscura memory...")
        ingested, skipped = self._ingestor.ingest(entries, force=force)
        print(f"  Memory: {ingested} sessions ingested, {skipped} already present")

    def ingest_only(self, agent: str | None = None) -> None:
        """Ingest sessions from existing INDEX.jsonl without re-syncing."""
        if not INDEX_FILE.is_file():
            print("No INDEX.jsonl found. Run sync first.", file=sys.stderr)
            return

        print("Loading sessions from INDEX.jsonl...")
        raw_entries = _safe_read_jsonl(INDEX_FILE)

        # Filter by agent if specified
        if agent:
            raw_entries = [e for e in raw_entries if e.get("agent") == agent]

        # Convert dicts back to SessionEntry objects
        entries: list[SessionEntry] = []
        for raw in raw_entries:
            entries.append(
                SessionEntry(
                    id=raw.get("id", ""),
                    agent=raw.get("agent", ""),
                    project=raw.get("project", ""),
                    model=raw.get("model", ""),
                    started=raw.get("started", ""),
                    ended=raw.get("ended", ""),
                    summary=raw.get("summary", ""),
                    message_count=raw.get("message_count", 0),
                    source_path=raw.get("source_path", ""),
                    synced_path=raw.get("synced_path", ""),
                    files=raw.get("files", []),
                    turns=raw.get("turns", []),
                    slug=raw.get("slug", ""),
                    cwds=raw.get("cwds", []),
                    git_branch=raw.get("git_branch", ""),
                    git_repo=raw.get("git_repo", ""),
                    tools_used=raw.get("tools_used", []),
                    agent_version=raw.get("agent_version", ""),
                    source=raw.get("source", ""),
                    topic=raw.get("topic", ""),
                )
            )

        print(f"  Loaded {len(entries)} sessions")
        self._ingest_to_memory(entries, force=True)

    def rebuild_index(self, agent: str | None = None) -> None:
        """Regenerate INDEX.jsonl from synced copies only."""
        agents_filter = [agent] if agent else None
        print("Rebuilding semantic index...")
        entries = self._indexer.build(agents=agents_filter)
        if not self.dry_run:
            self._indexer.write_index(entries)
        print(f"  INDEX.jsonl: {len(entries)} sessions indexed")

    def clean(self, agent: str | None = None) -> None:
        """Remove all synced data."""
        print("Cleaning synced session data...")
        self._cleaner.clean(agent=agent)
        print("Clean complete.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Agent session sync — discover, copy, and index agent sessions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 agent_sync.py                          # Sync all agents + ingest
  python3 agent_sync.py --agent copilot          # Sync copilot only
  python3 agent_sync.py --dry-run                # Preview changes
  python3 agent_sync.py --clean                  # Remove synced data
  python3 agent_sync.py --force                  # Force re-copy all
  python3 agent_sync.py --rebuild-index          # Regen INDEX.jsonl only
  python3 agent_sync.py --skip-memory            # Sync without memory ingest
  python3 agent_sync.py --ingest-only            # Ingest from existing INDEX
        """,
    )
    parser.add_argument(
        "--agent",
        choices=["claude", "copilot", "codex"],
        help="Sync specific agent only",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without changes",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove all synced session data",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-copy all sessions (ignore mtime)",
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="Regenerate INDEX.jsonl from synced copies",
    )
    parser.add_argument(
        "--skip-memory",
        action="store_true",
        help="Skip memory ingestion after sync",
    )
    parser.add_argument(
        "--ingest-only",
        action="store_true",
        help="Ingest from existing INDEX.jsonl without re-syncing",
    )

    args = parser.parse_args()
    sync = AgentSessionSync(dry_run=args.dry_run)

    if args.dry_run:
        print("DRY RUN — no changes will be made\n")

    if args.clean:
        sync.clean(agent=args.agent)
    elif args.rebuild_index:
        sync.rebuild_index(agent=args.agent)
    elif args.ingest_only:
        sync.ingest_only(agent=args.agent)
    else:
        sync.sync_all(
            agent=args.agent,
            force=args.force,
            skip_memory=args.skip_memory,
        )


if __name__ == "__main__":
    main()
