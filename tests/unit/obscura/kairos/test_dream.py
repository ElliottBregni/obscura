from __future__ import annotations

import datetime
import pathlib
import sqlite3
from pathlib import Path

from obscura.kairos.dream import DreamConsolidator


def test_sessions_since_counts(tmp_path: Path, monkeypatch: object) -> None:
    # Prepare a fake ~/.obscura/events.db in tmp_path
    home = tmp_path
    obdir = home / ".obscura"
    obdir.mkdir()
    db = obdir / "events.db"

    conn = sqlite3.connect(str(db))
    # sessions table with created_at stored as ISO text
    conn.execute("CREATE TABLE sessions (id TEXT, created_at TEXT)")

    now = datetime.datetime.now(datetime.UTC)
    older = (now - datetime.timedelta(hours=2)).isoformat()
    newer = (now - datetime.timedelta(minutes=10)).isoformat()

    conn.execute("INSERT INTO sessions (id, created_at) VALUES (?, ?)", ("s1", older))
    conn.execute("INSERT INTO sessions (id, created_at) VALUES (?, ?)", ("s2", newer))
    conn.commit()
    conn.close()

    # Monkeypatch Path.home() to point to our tmp home
    monkeypatch.setattr(pathlib.Path, "home", lambda: home)

    consolidator = DreamConsolidator()
    since_ts = (
        now - datetime.timedelta(hours=1)
    ).timestamp()  # should count only 'newer'
    count = consolidator._sessions_since(since_ts)
    assert count == 1
