from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from obscura.cli import main


def _seed_sessions_db(home: Path, rows: list[tuple[str, str]]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / "events.db"
    con = sqlite3.connect(str(db_path))
    con.execute("CREATE TABLE sessions (id TEXT, updated_at TEXT)")
    con.executemany("INSERT INTO sessions (id, updated_at) VALUES (?, ?)", rows)
    con.commit()
    con.close()


def test_continue_resumes_most_recent_session(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / ".obscura"
    _seed_sessions_db(
        home,
        [
            ("session-old", "2026-03-07T10:00:00"),
            ("session-new", "2026-03-08T10:00:00"),
        ],
    )
    monkeypatch.setenv("OBSCURA_HOME", str(home))

    repl_mock = AsyncMock()
    with patch("obscura.cli._repl", repl_mock):
        result = CliRunner().invoke(main, ["--continue"])

    assert result.exit_code == 0
    assert repl_mock.await_count == 1
    assert repl_mock.await_args.args[3] == "session-new"


def test_resume_takes_precedence_over_continue(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / ".obscura"
    _seed_sessions_db(home, [("session-db", "2026-03-08T10:00:00")])
    monkeypatch.setenv("OBSCURA_HOME", str(home))

    repl_mock = AsyncMock()
    with patch("obscura.cli._repl", repl_mock):
        result = CliRunner().invoke(main, ["--continue", "--resume", "session-cli"])

    assert result.exit_code == 0
    assert repl_mock.await_count == 1
    assert repl_mock.await_args.args[3] == "session-cli"

