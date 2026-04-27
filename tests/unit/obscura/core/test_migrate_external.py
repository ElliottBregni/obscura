"""Unit tests for obscura.core.migrate_external."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from obscura.core import migrate_external as me


# ---------------------------------------------------------------------------
# scan() detection
# ---------------------------------------------------------------------------


def test_scan_empty_dir_returns_nothing(tmp_path: Path) -> None:
    sources = me.scan(tmp_path, home=tmp_path / "home")
    assert sources == []


def test_scan_detects_cursor_rules_dir(tmp_path: Path) -> None:
    rules = tmp_path / ".cursor" / "rules"
    rules.mkdir(parents=True)
    (rules / "api.mdc").write_text("Always call validate().\n")
    (rules / "tests.md").write_text("Write unit tests.\n")

    sources = me.scan(tmp_path, home=tmp_path / "empty_home")
    ids = [s.id for s in sources]
    assert "cursor_project" in ids
    src = next(s for s in sources if s.id == "cursor_project")
    assert len(src.paths) == 2


def test_scan_detects_legacy_cursorrules(tmp_path: Path) -> None:
    (tmp_path / ".cursorrules").write_text("one rule")
    sources = me.scan(tmp_path, home=tmp_path / "h")
    assert any(s.id == "cursor_project" for s in sources)


def test_scan_detects_copilot_and_windsurf_and_gemini(tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "copilot-instructions.md").write_text("hi")
    (tmp_path / ".windsurfrules").write_text("w")
    (tmp_path / "GEMINI.md").write_text("g")

    ids = {s.id for s in me.scan(tmp_path, home=tmp_path / "h")}
    assert {"copilot_project", "windsurf_project", "gemini_project"} <= ids


def test_scan_detects_claude_commands(tmp_path: Path) -> None:
    cmd_dir = tmp_path / ".claude" / "commands"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "foo.md").write_text("---\ndescription: test\n---\nbody")

    sources = me.scan(tmp_path, home=tmp_path / "h")
    assert any(s.id == "claude_commands_project" for s in sources)


def test_scan_detects_mcp_config(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"demo": {"command": "x"}}}),
    )
    sources = me.scan(tmp_path, home=tmp_path / "h")
    assert any(s.id == "mcp_project" for s in sources)


# ---------------------------------------------------------------------------
# Migration primitives
# ---------------------------------------------------------------------------


def test_append_to_obscura_md_writes_and_is_idempotent(tmp_path: Path) -> None:
    src = tmp_path / ".cursorrules"
    src.write_text("RULE 1")

    # First call: appends.
    ok = me._append_to_obscura_md(tmp_path, [src], "Cursor")
    assert ok is True
    md = (tmp_path / "OBSCURA.md").read_text()
    assert "RULE 1" in md
    assert "Imported from Cursor" in md

    # Second call: no duplicate write (marker already present).
    ok2 = me._append_to_obscura_md(tmp_path, [src], "Cursor")
    assert ok2 is False
    md2 = (tmp_path / "OBSCURA.md").read_text()
    assert md2.count("RULE 1") == 1


def test_copy_commands_skips_existing(tmp_path: Path) -> None:
    src_dir = tmp_path / "src_cmd"
    src_dir.mkdir()
    a = src_dir / "a.md"
    a.write_text("alpha")
    b = src_dir / "b.md"
    b.write_text("beta")

    dest = tmp_path / "dest_cmd"
    # Pre-existing file with different content should NOT be overwritten.
    dest.mkdir()
    (dest / "a.md").write_text("KEEP ME")

    ok = me._copy_commands([a, b], dest)
    assert ok is True
    assert (dest / "a.md").read_text() == "KEEP ME"
    assert (dest / "b.md").read_text() == "beta"

    # Second call → nothing new to copy.
    ok2 = me._copy_commands([a, b], dest)
    assert ok2 is False


def test_merge_mcp_preserves_existing_keys(tmp_path: Path) -> None:
    src = tmp_path / ".mcp.json"
    src.write_text(json.dumps({"mcpServers": {"alpha": {"x": 1}, "beta": {"y": 2}}}))

    dest = tmp_path / "out" / "mcp.json"
    dest.parent.mkdir()
    dest.write_text(json.dumps({"mcpServers": {"alpha": {"existing": True}}}))

    ok = me._merge_mcp([src], dest)
    assert ok is True
    out = json.loads(dest.read_text())
    # Existing wins for alpha; beta imported.
    assert out["mcpServers"]["alpha"] == {"existing": True}
    assert out["mcpServers"]["beta"] == {"y": 2}

    # Second call → no new keys.
    ok2 = me._merge_mcp([src], dest)
    assert ok2 is False


def test_merge_mcp_accepts_snake_case(tmp_path: Path) -> None:
    src = tmp_path / "alt.json"
    src.write_text(json.dumps({"mcp_servers": {"gamma": {"z": 3}}}))
    dest = tmp_path / "out.json"
    ok = me._merge_mcp([src], dest)
    assert ok is True
    out = json.loads(dest.read_text())
    assert "gamma" in out["mcpServers"]


# ---------------------------------------------------------------------------
# Decision marker
# ---------------------------------------------------------------------------


def test_decision_roundtrip(tmp_path: Path) -> None:
    src = me.ExternalSource(
        id="test",
        label="Test",
        scope="project",
        dest="x",
        paths=[],
        migrate=None,
    )
    (tmp_path / ".obscura").mkdir()

    assert me._decision_for(src, tmp_path) is None
    me._record_decision(src, tmp_path, "imported")
    assert me._decision_for(src, tmp_path) == "imported"

    marker = tmp_path / ".obscura" / "state" / "external_migration.json"
    assert marker.is_file()
    data = json.loads(marker.read_text())
    assert data["decisions"]["test"]["status"] == "imported"
    assert "at" in data["decisions"]["test"]


def test_clear_decisions_removes_marker(tmp_path: Path) -> None:
    src = me.ExternalSource(
        id="test",
        label="T",
        scope="project",
        dest="x",
        paths=[],
        migrate=None,
    )
    (tmp_path / ".obscura").mkdir()
    me._record_decision(src, tmp_path, "never")
    assert me._decision_for(src, tmp_path) == "never"
    me.clear_decisions(tmp_path)
    assert me._decision_for(src, tmp_path) is None


# ---------------------------------------------------------------------------
# End-to-end migrate_all + startup wrapper
# ---------------------------------------------------------------------------


def test_migrate_all_records_outcomes(tmp_path: Path) -> None:
    (tmp_path / ".cursorrules").write_text("rule1")
    (tmp_path / ".obscura").mkdir()

    sources = [
        s for s in me.scan(tmp_path, home=tmp_path / "h") if s.scope == "project"
    ]
    assert sources, "cursor source should be detected"

    logs: list[str] = []
    imported = me.migrate_all(sources, tmp_path, emit=logs.append)
    assert imported == 1
    assert "rule1" in (tmp_path / "OBSCURA.md").read_text()

    # Decision recorded so it won't re-prompt.
    assert me._decision_for(sources[0], tmp_path) == "imported"


def test_run_startup_migration_disabled_by_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".cursorrules").write_text("x")
    monkeypatch.setenv("OBSCURA_EXTERNAL_MIGRATION", "0")

    logs: list[str] = []
    me.run_startup_migration(
        tmp_path,
        home=tmp_path / "h",
        interactive=False,
        print_fn=logs.append,
    )
    assert logs == []
    # Nothing written.
    assert not (tmp_path / "OBSCURA.md").exists()


def test_run_startup_migration_noninteractive_prints_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".cursorrules").write_text("x")
    monkeypatch.delenv("OBSCURA_EXTERNAL_MIGRATION", raising=False)

    logs: list[str] = []
    me.run_startup_migration(
        tmp_path,
        home=tmp_path / "h",
        interactive=False,
        print_fn=logs.append,
    )
    joined = "\n".join(logs)
    assert "Detected migratable" in joined
    assert "/migrate external" in joined
    # Non-interactive → no import happened.
    assert not (tmp_path / "OBSCURA.md").exists()


def test_run_startup_migration_skips_when_all_decided(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".cursorrules").write_text("x")
    (tmp_path / ".obscura").mkdir()
    # Pre-record decision so scan has nothing pending.
    src = next(s for s in me.scan(tmp_path, home=tmp_path / "h"))
    me._record_decision(src, tmp_path, "never")

    monkeypatch.delenv("OBSCURA_EXTERNAL_MIGRATION", raising=False)
    logs: list[str] = []
    me.run_startup_migration(
        tmp_path,
        home=tmp_path / "h",
        interactive=False,
        print_fn=logs.append,
    )
    assert logs == []
