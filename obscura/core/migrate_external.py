"""obscura.core.migrate_external — detect and migrate external agent configs.

On startup, Obscura scans for agent configs from neighbouring tools
(Cursor, Copilot, Windsurf, Gemini, Claude Code, Codex) at the project
and machine level.  If any are found and the user hasn't previously
decided, it prompts once and, with consent, imports them into the
canonical Obscura locations:

    Agent instructions  →  OBSCURA.md  (appended under a heading)
    Slash commands      →  .obscura/commands/   (or ~/.obscura/commands/)
    MCP servers         →  .obscura/mcp/mcp.json (or ~/.obscura/mcp/mcp.json)

Decisions are recorded per source id in a marker file so the prompt
never reappears for the same source.  ``CLAUDE.md`` and ``AGENTS.md``
are handled by :func:`obscura.cli._sync_guide_files` and are *not*
treated as external sources here.

Env vars:
    OBSCURA_EXTERNAL_MIGRATION=0    Disable the startup scan entirely.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from collections.abc import Callable

from obscura.core.paths import resolve_obscura_global_home

_log = logging.getLogger("obscura.migrate_external")

_MARKER_REL = Path("state") / "external_migration.json"
_OBSCURA_MD = "OBSCURA.md"
_DEST_COMMANDS = "commands"
_DEST_MCP = Path("mcp") / "mcp.json"


@dataclass
class ExternalSource:
    """A detected external agent config source."""

    id: str
    label: str
    scope: str  # "project" | "machine"
    dest: str
    paths: list[Path] = field(default_factory=lambda: cast(list[Path], []))
    migrate: Callable[[], bool] | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_startup_migration(
    cwd: Path | None = None,
    *,
    home: Path | None = None,
    interactive: bool = True,
    print_fn: Callable[[str], None] | None = None,
) -> None:
    """Scan for external agent configs and prompt once to import them.

    Safe to call unconditionally at CLI startup; returns immediately
    when disabled, when no sources are detected, or when every detected
    source has a recorded decision.
    """
    if os.environ.get("OBSCURA_EXTERNAL_MIGRATION", "1").strip() in {
        "0",
        "false",
        "no",
    }:
        return

    cwd = (cwd or Path.cwd()).resolve()
    sources = scan(cwd, home=home)
    if not sources:
        return

    pending = [s for s in sources if _decision_for(s, cwd) is None]
    if not pending:
        return

    emit = print_fn or _default_emit
    emit(_format_prompt(pending))

    if not interactive or not sys.stdin.isatty():
        emit(
            "  → Run `/migrate external` to import, "
            "or set OBSCURA_EXTERNAL_MIGRATION=0 to silence.",
        )
        return

    try:
        answer = input("Import now? [y/N/never] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        emit("")
        return

    if answer in {"n", "no", ""}:
        return
    if answer in {"never"}:
        for src in pending:
            _record_decision(src, cwd, "never")
        emit("Silenced. Use `/migrate external --force` to re-enable.")
        return
    if answer not in {"y", "yes"}:
        return

    migrate_all(pending, cwd, emit=emit)


def migrate_all(
    sources: list[ExternalSource],
    cwd: Path,
    *,
    emit: Callable[[str], None] | None = None,
) -> int:
    """Migrate each source, recording the outcome. Returns count imported."""
    emit = emit or _default_emit
    imported = 0
    for src in sources:
        ok = False
        if src.migrate is not None:
            try:
                ok = bool(src.migrate())
            except Exception as exc:  # pragma: no cover — defensive
                _log.warning("Migration failed for %s: %s", src.id, exc)
                emit(f"  ✗ {src.label}: {exc}")
                continue
        if ok:
            imported += 1
            _record_decision(src, cwd, "imported")
            emit(f"  ✓ {src.label} → {src.dest}")
        else:
            _record_decision(src, cwd, "skipped")
            emit(f"  · {src.label}: nothing new to import")
    return imported


def scan(cwd: Path | None = None, home: Path | None = None) -> list[ExternalSource]:
    """Enumerate external agent configs present for this project / machine."""
    cwd = (cwd or Path.cwd()).resolve()
    home = home or Path.home()
    sources: list[ExternalSource] = []
    for detect in _PROJECT_DETECTORS:
        src = detect(cwd)
        if src is not None:
            sources.append(src)
    for detect in _MACHINE_DETECTORS:
        src = detect(home)
        if src is not None:
            sources.append(src)
    return sources


# ---------------------------------------------------------------------------
# Project-scope detectors
# ---------------------------------------------------------------------------


def _detect_cursor_project(cwd: Path) -> ExternalSource | None:
    paths: list[Path] = []
    rules_dir = cwd / ".cursor" / "rules"
    if rules_dir.is_dir():
        for pattern in ("*.mdc", "*.md"):
            for entry in sorted(rules_dir.glob(pattern)):
                paths.append(entry)
    legacy = cwd / ".cursorrules"
    if legacy.is_file():
        paths.append(legacy)
    if not paths:
        return None
    return ExternalSource(
        id="cursor_project",
        label="Cursor rules",
        scope="project",
        dest="OBSCURA.md",
        paths=paths,
        migrate=lambda: _append_to_obscura_md(cwd, paths, "Cursor"),
    )


def _detect_copilot_project(cwd: Path) -> ExternalSource | None:
    path = cwd / ".github" / "copilot-instructions.md"
    if not path.is_file():
        return None
    return ExternalSource(
        id="copilot_project",
        label="GitHub Copilot instructions",
        scope="project",
        dest="OBSCURA.md",
        paths=[path],
        migrate=lambda: _append_to_obscura_md(cwd, [path], "GitHub Copilot"),
    )


def _detect_windsurf_project(cwd: Path) -> ExternalSource | None:
    path = cwd / ".windsurfrules"
    if not path.is_file():
        return None
    return ExternalSource(
        id="windsurf_project",
        label="Windsurf rules",
        scope="project",
        dest="OBSCURA.md",
        paths=[path],
        migrate=lambda: _append_to_obscura_md(cwd, [path], "Windsurf"),
    )


def _detect_gemini_project(cwd: Path) -> ExternalSource | None:
    path = cwd / "GEMINI.md"
    if not path.is_file():
        return None
    return ExternalSource(
        id="gemini_project",
        label="Gemini instructions",
        scope="project",
        dest="OBSCURA.md",
        paths=[path],
        migrate=lambda: _append_to_obscura_md(cwd, [path], "Gemini"),
    )


def _detect_claude_commands_project(cwd: Path) -> ExternalSource | None:
    src_dir = cwd / ".claude" / "commands"
    if not src_dir.is_dir():
        return None
    files = sorted(src_dir.glob("*.md"))
    if not files:
        return None
    dest_dir = cwd / ".obscura" / _DEST_COMMANDS
    return ExternalSource(
        id="claude_commands_project",
        label=f"Claude Code slash commands ({len(files)})",
        scope="project",
        dest=".obscura/commands/",
        paths=files,
        migrate=lambda: _copy_commands(files, dest_dir),
    )


def _detect_mcp_project(cwd: Path) -> ExternalSource | None:
    candidates = [
        cwd / ".mcp.json",
        cwd / ".claude" / "mcp.json",
        cwd / ".cursor" / "mcp.json",
    ]
    found = [p for p in candidates if p.is_file()]
    if not found:
        return None
    dest = cwd / ".obscura" / _DEST_MCP
    return ExternalSource(
        id="mcp_project",
        label=f"MCP server config ({len(found)})",
        scope="project",
        dest=str(Path(".obscura") / _DEST_MCP),
        paths=found,
        migrate=lambda: _merge_mcp(found, dest),
    )


_PROJECT_DETECTORS: list[Callable[[Path], ExternalSource | None]] = [
    _detect_cursor_project,
    _detect_copilot_project,
    _detect_windsurf_project,
    _detect_gemini_project,
    _detect_claude_commands_project,
    _detect_mcp_project,
]


# ---------------------------------------------------------------------------
# Machine-scope detectors
# ---------------------------------------------------------------------------


def _detect_claude_machine(home: Path) -> ExternalSource | None:
    path = home / ".claude" / "CLAUDE.md"
    if not path.is_file():
        return None
    dest_md = resolve_obscura_global_home() / _OBSCURA_MD
    return ExternalSource(
        id="claude_machine",
        label="Claude Code user instructions (~/.claude/CLAUDE.md)",
        scope="machine",
        dest="~/.obscura/OBSCURA.md",
        paths=[path],
        migrate=lambda: _append_to_file(dest_md, [path], "Claude Code (user)"),
    )


def _detect_claude_commands_machine(home: Path) -> ExternalSource | None:
    src_dir = home / ".claude" / "commands"
    if not src_dir.is_dir():
        return None
    files = sorted(src_dir.glob("*.md"))
    if not files:
        return None
    dest_dir = resolve_obscura_global_home() / _DEST_COMMANDS
    return ExternalSource(
        id="claude_commands_machine",
        label=f"Claude Code user commands ({len(files)})",
        scope="machine",
        dest="~/.obscura/commands/",
        paths=files,
        migrate=lambda: _copy_commands(files, dest_dir),
    )


def _detect_codex_machine(home: Path) -> ExternalSource | None:
    path = home / ".codex" / "AGENTS.md"
    if not path.is_file():
        return None
    dest_md = resolve_obscura_global_home() / _OBSCURA_MD
    return ExternalSource(
        id="codex_machine",
        label="Codex user AGENTS.md (~/.codex/AGENTS.md)",
        scope="machine",
        dest="~/.obscura/OBSCURA.md",
        paths=[path],
        migrate=lambda: _append_to_file(dest_md, [path], "Codex (user)"),
    )


def _detect_claude_desktop_mcp(home: Path) -> ExternalSource | None:
    candidates = [
        home
        / "Library"
        / "Application Support"
        / "Claude"
        / "claude_desktop_config.json",
        home / ".config" / "Claude" / "claude_desktop_config.json",
    ]
    found = [p for p in candidates if p.is_file()]
    if not found:
        return None
    dest = resolve_obscura_global_home() / _DEST_MCP
    return ExternalSource(
        id="claude_desktop_mcp",
        label="Claude Desktop MCP servers",
        scope="machine",
        dest="~/.obscura/mcp/mcp.json",
        paths=found,
        migrate=lambda: _merge_mcp(found, dest),
    )


_MACHINE_DETECTORS: list[Callable[[Path], ExternalSource | None]] = [
    _detect_claude_machine,
    _detect_claude_commands_machine,
    _detect_codex_machine,
    _detect_claude_desktop_mcp,
]


# ---------------------------------------------------------------------------
# Migration primitives
# ---------------------------------------------------------------------------


def _append_to_obscura_md(cwd: Path, paths: list[Path], label: str) -> bool:
    return _append_to_file(cwd / _OBSCURA_MD, paths, label)


def _append_to_file(dest: Path, paths: list[Path], label: str) -> bool:
    """Append each source's content to *dest* under a labelled section.

    Idempotent per source: if the dest already contains a section whose
    marker matches the source path, that source is skipped.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    existing = dest.read_text(encoding="utf-8") if dest.is_file() else ""

    appended = False
    buf: list[str] = []
    for src in paths:
        marker = _section_marker(src)
        if marker in existing:
            continue
        try:
            body = src.read_text(encoding="utf-8")
        except OSError as exc:
            _log.debug("Cannot read %s: %s", src, exc)
            continue
        heading = f"## Imported from {label} — {src.name}"
        buf.append(f"\n\n<!-- {marker} -->\n{heading}\n\n{body.strip()}\n")
        appended = True

    if not appended:
        return False

    with dest.open("a", encoding="utf-8") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write("".join(buf))
    return True


def _copy_commands(files: list[Path], dest_dir: Path) -> bool:
    """Copy each command file into dest_dir. Skips any that already exist."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in files:
        target = dest_dir / src.name
        if target.exists():
            continue
        try:
            shutil.copy2(src, target)
        except OSError as exc:
            _log.debug("Cannot copy %s: %s", src, exc)
            continue
        copied += 1
    return copied > 0


def _merge_mcp(sources: list[Path], dest: Path) -> bool:
    """Merge mcpServers from each source into dest. Existing keys win."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    current: dict[str, Any] = {"mcpServers": {}}
    if dest.is_file():
        try:
            parsed: Any = json.loads(dest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _log.debug("Cannot parse %s: %s", dest, exc)
        else:
            parsed_dict = _coerce_str_keys(parsed)
            if parsed_dict:
                current = parsed_dict
    servers_out: dict[str, Any] = _coerce_str_keys(current.get("mcpServers"))
    current["mcpServers"] = servers_out

    added = 0
    for src in sources:
        try:
            data: Any = json.loads(src.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _log.debug("Cannot parse %s: %s", src, exc)
            continue
        if not isinstance(data, dict):
            continue
        data_d = cast(dict[str, Any], data)
        raw_in: Any = data_d.get("mcpServers") or data_d.get("mcp_servers")
        servers_in = _coerce_str_keys(raw_in)
        if not servers_in:
            continue
        for key, cfg in servers_in.items():
            if key in servers_out:
                continue
            servers_out[key] = cfg
            added += 1

    if added == 0:
        return False

    dest.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Decision marker
# ---------------------------------------------------------------------------


def _marker_path(scope: str, cwd: Path) -> Path:
    if scope == "project":
        return cwd / ".obscura" / _MARKER_REL
    return resolve_obscura_global_home() / _MARKER_REL


def _load_marker(scope: str, cwd: Path) -> dict[str, Any]:
    path = _marker_path(scope, cwd)
    if not path.is_file():
        return {}
    try:
        parsed: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return _coerce_str_keys(parsed)


def _save_marker(scope: str, cwd: Path, data: dict[str, Any]) -> None:
    path = _marker_path(scope, cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _decision_for(src: ExternalSource, cwd: Path) -> str | None:
    data = _load_marker(src.scope, cwd)
    decisions = _coerce_str_keys(data.get("decisions"))
    entry = _coerce_str_keys(decisions.get(src.id))
    status: Any = entry.get("status")
    return status if isinstance(status, str) else None


def _record_decision(src: ExternalSource, cwd: Path, status: str) -> None:
    data = _load_marker(src.scope, cwd)
    decisions: dict[str, Any] = _coerce_str_keys(data.get("decisions"))
    data["decisions"] = decisions
    decisions[src.id] = {
        "status": status,
        "at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
    }
    _save_marker(src.scope, cwd, data)


def clear_decisions(cwd: Path | None = None) -> None:
    """Remove all recorded decisions so the prompt will re-appear."""
    cwd = (cwd or Path.cwd()).resolve()
    for scope in ("project", "machine"):
        path = _marker_path(scope, cwd)
        if path.is_file():
            path.unlink()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _coerce_str_keys(value: Any) -> dict[str, Any]:
    """Return a shallow copy with all keys coerced to ``str``.

    Accepts ``Any`` so callers don't have to narrow into ``dict[Unknown, …]``
    under pyright strict.  Non-dict inputs yield an empty result.
    """
    if not isinstance(value, dict):
        return {}
    casted = cast(dict[Any, Any], value)
    return {str(k): v for k, v in casted.items()}


def _section_marker(src: Path) -> str:
    return f"obscura:external-migration:{src.as_posix()}"


def _format_prompt(sources: list[ExternalSource]) -> str:
    lines = ["", "Detected migratable external agent config:"]
    for s in sources:
        lines.append(f"  • {s.label}  →  {s.dest}")
    lines.append("")
    return "\n".join(lines)


def _default_emit(msg: str) -> None:
    print(msg, file=sys.stderr)


__all__ = [
    "ExternalSource",
    "clear_decisions",
    "migrate_all",
    "run_startup_migration",
    "scan",
]
