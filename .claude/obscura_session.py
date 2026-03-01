"""
Helper to integrate Claude Code with Obscura's session tracking system.
Logs all work to .obscura/events.db for continuity across sessions.
"""
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SESSION_ID = "cbd9d92729114dde8799b9ad0b5b6d68"
DB_PATH = Path(__file__).parent.parent / ".obscura" / "events.db"


def log_event(kind: str, payload: dict[str, Any]) -> None:
    """Log an event to the current Obscura session."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Get next sequence number
    seq = cursor.execute(
        "SELECT COALESCE(MAX(seq), -1) + 1 FROM events WHERE session_id = ?",
        (SESSION_ID,)
    ).fetchone()[0]
    
    # Insert event
    cursor.execute("""
        INSERT INTO events (session_id, seq, kind, payload, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, (
        SESSION_ID,
        seq,
        kind,
        json.dumps(payload),
        datetime.now(timezone.utc).isoformat()
    ))
    
    # Update session timestamp
    cursor.execute("""
        UPDATE sessions SET updated_at = ? WHERE id = ?
    """, (datetime.now(timezone.utc).isoformat(), SESSION_ID))
    
    conn.commit()
    conn.close()


def complete_session() -> None:
    """Mark the current session as completed."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE sessions 
        SET status = 'completed', updated_at = ?
        WHERE id = ?
    """, (datetime.now(timezone.utc).isoformat(), SESSION_ID))
    conn.commit()
    conn.close()


def get_session_context() -> dict[str, Any]:
    """Retrieve the current session's context from all events."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    events = cursor.execute("""
        SELECT kind, payload, timestamp
        FROM events
        WHERE session_id = ?
        ORDER BY seq
    """, (SESSION_ID,)).fetchall()
    
    conn.close()
    
    return {
        "session_id": SESSION_ID,
        "event_count": len(events),
        "events": [
            {"kind": e[0], "payload": json.loads(e[1]), "timestamp": e[2]}
            for e in events
        ]
    }


if __name__ == "__main__":
    # Example usage
    log_event("tool_call", {
        "tool": "bash",
        "description": "Testing Obscura session integration"
    })
    print(f"Logged event to session {SESSION_ID}")
    print(f"\nSession context: {json.dumps(get_session_context(), indent=2)}")
