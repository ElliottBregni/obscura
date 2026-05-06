"""Tests for vault session-log export.

Covers:
  - Per-session pages: structure, tool counts, goal backlinks, sweep
    of stale pages.
  - Rolling digest: combines event-store sessions + deep-log JSONL,
    renders top tools and recent-session backlinks.
  - export_session_logs returns the total count and writes both flavors.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import yaml

from obscura.core.enums.agent import AgentEventKind
from obscura.kairos.vault_sync import VaultSync


def _split(content: str) -> tuple[str, dict[str, Any]]:
    assert content.startswith("---\n"), content[:40]
    rest = content[4:]
    end = rest.index("---")
    fm_text = rest[:end]
    body = rest[end + 4 :]
    fm = yaml.safe_load(fm_text) or {}
    return body, fm


def _make_session(
    sid: str,
    *,
    backend: str = "copilot",
    model: str = "gpt-4",
    agent: str = "default",
    project: str = "obscura",
    summary: str = "",
    message_count: int = 4,
    goal_id: str = "",
    updated_offset_minutes: int = 0,
) -> SimpleNamespace:
    """Build a SessionRecord-shaped SimpleNamespace for tests."""
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    metadata: dict[str, Any] = {}
    if goal_id:
        metadata["goal_id"] = goal_id
    return SimpleNamespace(
        id=sid,
        backend=backend,
        model=model,
        active_agent=agent,
        project=project,
        summary=summary,
        message_count=message_count,
        metadata=metadata,
        status=SimpleNamespace(value="completed"),
        created_at=base,
        updated_at=datetime(
            2026, 5, 1, 12, updated_offset_minutes, 0, tzinfo=UTC
        ),
    )


def _make_event(kind: AgentEventKind, **payload: Any) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, payload=payload)


# ---------------------------------------------------------------------------
# _render_session_page
# ---------------------------------------------------------------------------


class TestRenderSessionPage:
    def test_basic_session_renders_with_full_frontmatter(
        self, tmp_path: Path
    ) -> None:
        sync = VaultSync(vault_dir=tmp_path)
        sess = _make_session(
            "sess-1",
            summary="Refactor the event store",
            goal_id="g-events",
        )

        # No events -> turn_count=0, tool_call_count=0.
        class _StoreNoEvents:
            async def get_events(self, _sid: str) -> list[Any]:
                return []

            async def list_sessions(self) -> list[Any]:
                return []

            def close(self) -> None:
                pass

        with patch(
            "obscura.core.db_factory.DatabaseFactory.create_event_store",
            return_value=_StoreNoEvents(),
        ):
            result = sync._render_session_page(sess)  # pyright: ignore[reportPrivateUsage]
        assert result is not None
        page_id, content = result
        assert page_id == "sess-1"
        body, fm = _split(content)
        assert fm["id"] == "sess-1"
        assert fm["type"] == "session"
        assert fm["agent"] == "default"
        assert fm["backend"] == "copilot"
        assert fm["model"] == "gpt-4"
        assert fm["status"] == "completed"
        assert fm["message_count"] == 4
        assert fm["turn_count"] == 0
        assert fm["tool_call_count"] == 0
        assert fm["goal_id"] == "g-events"
        # Body has the goal backlink under '## Goal'.
        assert "[[../../agent/goals/g-events]]" in body
        assert "Refactor the event store" in body

    def test_session_with_events_counts_turns_and_tools(
        self, tmp_path: Path
    ) -> None:
        sync = VaultSync(vault_dir=tmp_path)
        sess = _make_session("sess-2")

        events = [
            _make_event(AgentEventKind.TURN_COMPLETE),
            _make_event(AgentEventKind.TOOL_CALL, tool_name="read"),
            _make_event(AgentEventKind.TOOL_CALL, tool_name="read"),
            _make_event(AgentEventKind.TOOL_CALL, tool_name="grep"),
            _make_event(AgentEventKind.TURN_COMPLETE),
        ]

        class _Store:
            def __init__(self) -> None:
                self.calls = 0

            async def get_events(self, _sid: str) -> list[Any]:
                return events

            def list_sessions(self) -> list[Any]:
                return []

            def close(self) -> None:
                pass

        with patch(
            "obscura.core.db_factory.DatabaseFactory.create_event_store",
            return_value=_Store(),
        ):
            result = sync._render_session_page(sess)  # pyright: ignore[reportPrivateUsage]
        assert result is not None
        body, fm = _split(result[1])
        assert fm["turn_count"] == 2
        assert fm["tool_call_count"] == 3
        # Body's tools section orders by count desc, ties by name asc.
        tools_section = body.split("## Tools used", 1)[1]
        assert tools_section.index("`read` — 2") < tools_section.index("`grep` — 1")

    def test_session_without_id_returns_none(self, tmp_path: Path) -> None:
        sync = VaultSync(vault_dir=tmp_path)
        sess = SimpleNamespace(id="")
        result = sync._render_session_page(sess)  # pyright: ignore[reportPrivateUsage]
        assert result is None


# ---------------------------------------------------------------------------
# _export_session_pages: sweep + cap
# ---------------------------------------------------------------------------


class TestExportSessionPages:
    def test_writes_one_file_per_session_and_sweeps_stale(
        self, tmp_path: Path
    ) -> None:
        sync = VaultSync(vault_dir=tmp_path)
        sessions_dir = tmp_path / "shared" / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        # Pre-existing stale per-session page that should be swept.
        stale = sessions_dir / "sess-old.md"
        stale.write_text("---\nid: sess-old\n---\n")
        # Pre-existing digest page that must SURVIVE the sweep.
        digest = sessions_dir / "recent-activity.md"
        digest.write_text("---\nid: recent-activity\n---\n")

        live = [
            _make_session("sess-a", updated_offset_minutes=10),
            _make_session("sess-b", updated_offset_minutes=20),
        ]

        class _Store:
            async def list_sessions(self) -> list[Any]:
                return live

            async def get_events(self, _sid: str) -> list[Any]:
                return []

            def close(self) -> None:
                pass

        with patch(
            "obscura.core.db_factory.DatabaseFactory.create_event_store",
            return_value=_Store(),
        ):
            written = sync._export_session_pages()  # pyright: ignore[reportPrivateUsage]

        assert written == 2
        assert (sessions_dir / "sess-a.md").exists()
        assert (sessions_dir / "sess-b.md").exists()
        # Stale per-session page swept; digest preserved.
        assert not stale.exists()
        assert digest.exists()


# ---------------------------------------------------------------------------
# _scan_deep_log_tail: standalone helper
# ---------------------------------------------------------------------------


class TestScanDeepLogTail:
    def test_missing_log_returns_zero_stats(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        # Point home dir at an empty tmp path so no deep.jsonl exists.
        from unittest.mock import patch as _patch

        with _patch("pathlib.Path.home", return_value=tmp_path):
            stats = VaultSync._scan_deep_log_tail(100)
        assert stats["scanned"] == 0
        assert stats["tool_calls"] == 0
        assert stats["api_requests"] == 0
        assert stats["errors"] == 0
        assert stats["by_tool"] == {}

    def test_counts_by_kind_and_tool(
        self, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / ".obscura" / "logs"
        log_dir.mkdir(parents=True)
        log_path = log_dir / "deep.jsonl"
        entries = [
            {"type": "tool_call", "data": {"tool": "read"}},
            {"type": "tool_call", "data": {"tool": "read"}},
            {"type": "tool_call", "data": {"tool": "grep"}},
            {"type": "api_request", "data": {"model": "gpt-4"}},
            {"type": "api_request", "data": {"model": "gpt-4"}},
            {"type": "error", "data": {"message": "boom"}},
            {"type": "session", "data": {"action": "start"}},  # ignored
        ]
        log_path.write_text(
            "\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8"
        )

        from unittest.mock import patch as _patch

        with _patch("pathlib.Path.home", return_value=tmp_path):
            stats = VaultSync._scan_deep_log_tail(100)
        assert stats["scanned"] == 7
        assert stats["tool_calls"] == 3
        assert stats["api_requests"] == 2
        assert stats["errors"] == 1
        assert stats["by_tool"] == {"read": 2, "grep": 1}


# ---------------------------------------------------------------------------
# _export_session_digest
# ---------------------------------------------------------------------------


class TestExportSessionDigest:
    def test_digest_includes_log_stats_and_session_backlinks(
        self, tmp_path: Path
    ) -> None:
        # Set up a deep log under tmp_path so _scan_deep_log_tail finds it.
        log_dir = tmp_path / ".obscura" / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "deep.jsonl").write_text(
            "\n".join(
                json.dumps(e)
                for e in (
                    {"type": "tool_call", "data": {"tool": "read"}},
                    {"type": "api_request", "data": {}},
                )
            )
            + "\n",
            encoding="utf-8",
        )

        sync = VaultSync(vault_dir=tmp_path / "vault")
        sessions = [
            _make_session("sess-x", summary="Investigate vault", updated_offset_minutes=5),
            _make_session("sess-y", summary="Fix login bug", updated_offset_minutes=10),
        ]

        class _Store:
            async def list_sessions(self) -> list[Any]:
                return sessions

            def close(self) -> None:
                pass

        from unittest.mock import patch as _patch

        with (
            patch(
                "obscura.core.db_factory.DatabaseFactory.create_event_store",
                return_value=_Store(),
            ),
            _patch("pathlib.Path.home", return_value=tmp_path),
        ):
            written = sync._export_session_digest()  # pyright: ignore[reportPrivateUsage]

        assert written == 1
        digest = (
            tmp_path / "vault" / "shared" / "sessions" / "recent-activity.md"
        ).read_text()
        body, fm = _split(digest)
        assert fm["type"] == "session_digest"
        assert fm["log_tool_calls"] == 1
        assert fm["log_api_requests"] == 1
        # Session backlinks render as [[<sid>]] within the same dir.
        assert "[[sess-x]]" in body
        assert "[[sess-y]]" in body
        # Top-tools section appears.
        assert "`read` — 1" in body


# ---------------------------------------------------------------------------
# export_session_logs: public entry point
# ---------------------------------------------------------------------------


class TestExportSessionLogs:
    def test_returns_combined_count(self, tmp_path: Path) -> None:
        sync = VaultSync(vault_dir=tmp_path / "v")

        class _Store:
            async def list_sessions(self) -> list[Any]:
                return [_make_session("only-one")]

            async def get_events(self, _sid: str) -> list[Any]:
                return []

            def close(self) -> None:
                pass

        with patch(
            "obscura.core.db_factory.DatabaseFactory.create_event_store",
            return_value=_Store(),
        ):
            count = sync.export_session_logs()
        # 1 per-session page + 1 digest = 2.
        assert count == 2
        assert (tmp_path / "v" / "shared" / "sessions" / "only-one.md").exists()
        assert (
            tmp_path / "v" / "shared" / "sessions" / "recent-activity.md"
        ).exists()
