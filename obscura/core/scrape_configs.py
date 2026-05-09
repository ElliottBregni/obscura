"""Scan known agent-config locations and import their skills + MCP servers.

Covers what ``migrate_external.py`` doesn't:
  - Skill folders from ``~/.claude/skills``, ``~/.copilot/skills``,
    ``~/.codex/skills``, ``~/.config/opencode/skill``.
  - Kiro "powers" (``~/.kiro/powers/installed/<name>/`` — ``POWER.md`` +
    optional ``mcp.json`` + ``steering/``).
  - MCP server entries embedded in non-standard formats: codex TOML
    (``[mcp_servers.X]``), opencode (``mcp`` key), copilot
    (``mcp-config.json``), claude desktop / settings.

Normalizes transport quirks (``type: local`` → ``transport: stdio``)
that break obscura's loader, and skips entries whose name is already
present in obscura.
"""

from __future__ import annotations

import json
import logging
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

_logger = logging.getLogger(__name__)

SourceKind = Literal[
    "skill_dir",  # parent dir holding folder-skills (each child has SKILL.md)
    "kiro_powers",  # parent dir of installed kiro powers (POWER.md)
    "mcp_json",  # JSON file with `mcpServers` (claude/copilot/kiro)
    "mcp_toml",  # codex config.toml with [mcp_servers.X] tables
    "opencode_json",  # opencode.json with `mcp` key
]


@dataclass(frozen=True)
class Source:
    """A known agent config location."""

    label: str
    kind: SourceKind
    path: Path
    skill_prefix: str = ""  # prepended to scraped skill folder names


def known_sources(home: Path | None = None) -> list[Source]:
    """Return the canonical list of agent config sources to scrape."""
    h = home or Path.home()
    return [
        # ---- skill folders ----
        Source("claude", "skill_dir", h / ".claude" / "skills"),
        Source("copilot", "skill_dir", h / ".copilot" / "skills"),
        Source("codex", "skill_dir", h / ".codex" / "skills"),
        Source("opencode", "skill_dir", h / ".config" / "opencode" / "skill"),
        # ---- kiro powers (skill + mcp combined) ----
        Source(
            "kiro",
            "kiro_powers",
            h / ".kiro" / "powers" / "installed",
            skill_prefix="kiro-",
        ),
        # ---- MCP-only sources ----
        Source("claude-settings", "mcp_json", h / ".claude" / "settings.json"),
        Source("copilot-mcp", "mcp_json", h / ".copilot" / "mcp-config.json"),
        Source("codex-toml", "mcp_toml", h / ".codex" / "config.toml"),
        Source(
            "opencode-config",
            "opencode_json",
            h / ".config" / "opencode" / "opencode.json",
        ),
        Source(
            "claude-desktop",
            "mcp_json",
            h
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json",
        ),
    ]


@dataclass
class ScanReport:
    """What scanning found, before applying."""

    skills_new: list[tuple[Source, Path, str]] = field(
        default_factory=lambda: cast("list[tuple[Source, Path, str]]", []),
    )
    skills_skipped: list[tuple[Source, Path, str]] = field(
        default_factory=lambda: cast("list[tuple[Source, Path, str]]", []),
    )
    mcps_new: list[tuple[Source, str, dict[str, Any]]] = field(
        default_factory=lambda: cast(
            "list[tuple[Source, str, dict[str, Any]]]",
            [],
        ),
    )
    mcps_skipped: list[tuple[Source, str, str]] = field(
        default_factory=lambda: cast("list[tuple[Source, str, str]]", []),
    )
    sources_missing: list[Source] = field(
        default_factory=lambda: cast("list[Source]", []),
    )
    errors: list[tuple[Source, str]] = field(
        default_factory=lambda: cast("list[tuple[Source, str]]", []),
    )

    @property
    def has_changes(self) -> bool:
        return bool(self.skills_new) or bool(self.mcps_new)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def scan(
    *,
    home: Path | None = None,
    obscura_home: Path | None = None,
    sources: list[Source] | None = None,
) -> ScanReport:
    """Survey configured sources. Pure: no writes."""
    h = home or Path.home()
    obscura = obscura_home or (h / ".obscura")
    src_list = sources if sources is not None else known_sources(h)

    skills_dir = obscura / "skills"
    mcp_path = obscura / "mcp" / "mcp.json"
    existing_skills = _existing_skill_names(skills_dir)
    existing_mcps = _existing_mcp_names(mcp_path)

    report = ScanReport()

    for src in src_list:
        if not src.path.exists():
            report.sources_missing.append(src)
            continue
        try:
            if src.kind == "skill_dir":
                _scan_skill_dir(src, existing_skills, report)
            elif src.kind == "kiro_powers":
                _scan_kiro_powers(src, existing_skills, existing_mcps, report)
            elif src.kind in ("mcp_json", "opencode_json"):
                _scan_mcp_json(src, existing_mcps, report)
            elif src.kind == "mcp_toml":
                _scan_mcp_toml(src, existing_mcps, report)
        except Exception as exc:  # noqa: BLE001 — surfaced as report error
            _logger.debug("scrape error from %s", src, exc_info=True)
            report.errors.append((src, f"{type(exc).__name__}: {exc}"))

    return report


def _existing_skill_names(skills_dir: Path) -> set[str]:
    if not skills_dir.is_dir():
        return set()
    return {p.stem if p.is_file() else p.name for p in skills_dir.iterdir()}


def _existing_mcp_names(mcp_path: Path) -> set[str]:
    if not mcp_path.is_file():
        return set()
    try:
        data = json.loads(mcp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _logger.debug(
            "scrape: skipping unreadable mcp config %s",
            mcp_path,
            exc_info=True,
        )
        return set()
    if not isinstance(data, dict):
        return set()
    data_d = cast("dict[str, Any]", data)
    servers = data_d.get("mcpServers")
    if not isinstance(servers, dict):
        return set()
    return set(cast("dict[str, Any]", servers).keys())


def _scan_skill_dir(src: Source, existing: set[str], report: ScanReport) -> None:
    for entry in sorted(src.path.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        target_name = f"{src.skill_prefix}{entry.name}"
        if target_name in existing:
            report.skills_skipped.append((src, entry, "duplicate"))
        else:
            report.skills_new.append((src, entry, target_name))


def _scan_kiro_powers(
    src: Source,
    existing_skills: set[str],
    existing_mcps: set[str],
    report: ScanReport,
) -> None:
    for power_dir in sorted(src.path.iterdir()):
        if not power_dir.is_dir() or power_dir.name.startswith("."):
            continue
        target_skill = f"{src.skill_prefix}{power_dir.name}"
        if (power_dir / "POWER.md").is_file():
            if target_skill in existing_skills:
                report.skills_skipped.append((src, power_dir, "duplicate"))
            else:
                report.skills_new.append((src, power_dir, target_skill))

        mcp_json = power_dir / "mcp.json"
        if mcp_json.is_file():
            for name, cfg in _read_mcp_servers_json(mcp_json).items():
                normalized = _normalize_mcp_entry(cfg)
                if name in existing_mcps:
                    report.mcps_skipped.append((src, name, "duplicate"))
                else:
                    report.mcps_new.append((src, name, normalized))


def _scan_mcp_json(src: Source, existing: set[str], report: ScanReport) -> None:
    raw = json.loads(src.path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return
    raw_d = cast("dict[str, Any]", raw)
    if src.kind == "opencode_json":
        servers_raw: Any = raw_d.get("mcp") or raw_d.get("mcpServers") or {}
    else:
        servers_raw = raw_d.get("mcpServers") or raw_d.get("mcp_servers") or {}
    if not isinstance(servers_raw, dict):
        return
    for name, cfg in cast("dict[str, Any]", servers_raw).items():
        if not isinstance(cfg, dict):
            continue
        normalized = _normalize_mcp_entry(cast("dict[str, Any]", cfg))
        if str(name) in existing:
            report.mcps_skipped.append((src, str(name), "duplicate"))
        else:
            report.mcps_new.append((src, str(name), normalized))


def _scan_mcp_toml(src: Source, existing: set[str], report: ScanReport) -> None:
    parsed = tomllib.loads(src.path.read_text(encoding="utf-8"))
    servers: Any = parsed.get("mcp_servers") or parsed.get("mcpServers") or {}
    if not isinstance(servers, dict):
        return
    for name, cfg in cast("dict[str, Any]", servers).items():
        if not isinstance(cfg, dict):
            continue
        normalized = _normalize_mcp_entry(cast("dict[str, Any]", cfg))
        if str(name) in existing:
            report.mcps_skipped.append((src, str(name), "duplicate"))
        else:
            report.mcps_new.append((src, str(name), normalized))


def _read_mcp_servers_json(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _logger.debug("scrape: skipping unreadable %s", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    servers = cast("dict[str, Any]", data).get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    return {
        str(k): cast("dict[str, Any]", v)
        for k, v in cast("dict[str, Any]", servers).items()
        if isinstance(v, dict)
    }


def _normalize_mcp_entry(cfg: dict[str, Any]) -> dict[str, Any]:
    """Translate provider-specific quirks into obscura's canonical shape.

    - Claude's ``type: local`` → ``transport: stdio`` (obscura rejects "local")
    - ``type`` (Claude/Kiro) → ``transport`` (obscura native)
    - HTTP/SSE entries with no transport but a ``url`` → ``transport: http``
    """
    out = dict(cfg)
    raw_type = out.pop("type", None)
    if raw_type and "transport" not in out:
        t = str(raw_type).lower()
        if t == "local":
            out["transport"] = "stdio"
        elif t in ("stdio", "sse", "http"):
            out["transport"] = t
        else:
            out["transport"] = "stdio"
    if "transport" not in out:
        out["transport"] = "http" if out.get("url") else "stdio"
    return out


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply(
    report: ScanReport,
    *,
    obscura_home: Path | None = None,
) -> tuple[int, int]:
    """Execute the changes in *report*. Returns (skills_added, mcps_added)."""
    obscura = obscura_home or (Path.home() / ".obscura")
    skills_dir = obscura / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    mcp_path = obscura / "mcp" / "mcp.json"
    mcp_path.parent.mkdir(parents=True, exist_ok=True)

    skills_added = 0
    for src, entry, target_name in report.skills_new:
        dest = skills_dir / target_name
        if dest.exists():
            continue
        if src.kind == "kiro_powers":
            _install_kiro_skill(entry, dest)
        else:
            shutil.copytree(entry, dest)
        skills_added += 1

    mcps_added = 0
    if report.mcps_new:
        current: dict[str, Any] = {"mcpServers": {}}
        if mcp_path.is_file():
            try:
                parsed = json.loads(mcp_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    current = cast("dict[str, Any]", parsed)
            except json.JSONDecodeError:
                _logger.debug(
                    "scrape: invalid JSON in %s, treating as empty",
                    mcp_path,
                    exc_info=True,
                )
        servers = current.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            servers = {}
            current["mcpServers"] = servers
        servers_d = cast("dict[str, Any]", servers)
        for _, name, cfg in report.mcps_new:
            if name in servers_d:
                continue
            servers_d[name] = cfg
            mcps_added += 1
        mcp_path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")

    return skills_added, mcps_added


def _install_kiro_skill(power_dir: Path, dest: Path) -> None:
    """Convert a kiro power directory into an obscura skill directory."""
    dest.mkdir(parents=True)
    power_md = power_dir / "POWER.md"
    if power_md.is_file():
        shutil.copyfile(power_md, dest / "SKILL.md")
    steering = power_dir / "steering"
    if steering.is_dir():
        shutil.copytree(steering, dest / "steering")
    mcp_json = power_dir / "mcp.json"
    if mcp_json.is_file():
        shutil.copyfile(mcp_json, dest / "mcp.json")
