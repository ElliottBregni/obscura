"""obscura.cli._guide_sync — Startup workspace-guide file sync.

Keeps OBSCURA.md <-> AGENTS.md in sync (cross-tool standard used by Codex,
Cursor, Aider, etc.) and writes provider-local permission bypass settings
so Obscura's policy engine is the single source of truth.

Public API
----------
sync_guide_files()        -- OBSCURA.md <-> AGENTS.md bi-directional sync
sync_provider_settings()  -- Write .claude/settings.local.json bypass file
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger("obscura.cli")


def sync_guide_files() -> None:
    """Keep OBSCURA.md and AGENTS.md in sync at startup.

    AGENTS.md is the cross-tool standard (Codex, Cursor, Aider, etc.) that
    pairs with OBSCURA.md. Rules:
      - OBSCURA.md exists -> overwrite AGENTS.md with its content.
      - OBSCURA.md missing, AGENTS.md exists -> create OBSCURA.md from it.
      - Both missing, legacy CLAUDE.md exists -> bootstrap OBSCURA.md +
        AGENTS.md from CLAUDE.md (one-time migration; CLAUDE.md is left
        untouched for tools that still consume it).
      - Nothing exists -> no-op.

    Only operates on the current working directory.  Failures are
    silently logged -- this must never block startup.
    """
    cwd = Path.cwd()
    obscura_md = cwd / "OBSCURA.md"
    agents_md = cwd / "AGENTS.md"
    claude_md = cwd / "CLAUDE.md"

    try:
        if obscura_md.is_file():
            content = obscura_md.read_text(encoding="utf-8")
            agents_md.write_text(content, encoding="utf-8")
            _log.debug("Synced AGENTS.md <- OBSCURA.md")
        elif agents_md.is_file():
            content = agents_md.read_text(encoding="utf-8")
            obscura_md.write_text(content, encoding="utf-8")
            _log.debug("Created OBSCURA.md <- AGENTS.md")
        elif claude_md.is_file():
            content = claude_md.read_text(encoding="utf-8")
            obscura_md.write_text(content, encoding="utf-8")
            agents_md.write_text(content, encoding="utf-8")
            _log.debug("Bootstrapped OBSCURA.md + AGENTS.md from legacy CLAUDE.md")
    except OSError as exc:
        _log.debug("Guide file sync failed: %s", exc)


def sync_provider_settings() -> None:
    """Write provider-specific settings to disable their permission layers.

    Obscura has its own tool-policy engine (``obscura/tools/policy/``).
    When running inside a provider like Claude Code, the provider's
    sandbox adds a *second* permission layer that duplicates -- and often
    blocks -- operations Obscura has already authorised.

    This function writes a provider-local settings file that fully
    disables the outer permission layer so Obscura's policy engine is the
    single source of truth.  Currently supports:

      - **Claude Code** -> ``.claude/settings.local.json``

    The file is ``.local.json`` (gitignored by convention), so it never
    leaks into the repo.  Failures are silently logged.
    """
    import json

    cwd = Path.cwd()

    # --- Claude Code --------------------------------------------------
    claude_dir = cwd / ".claude"
    settings_file = claude_dir / "settings.local.json"

    desired: dict[str, Any] = {
        "permissions": {
            "allow": [
                "Bash(*)",
                "Read(*)",
                "Write(*)",
                "Edit(*)",
                "Glob(*)",
                "Grep(*)",
                "WebFetch(*)",
                "WebSearch(*)",
                "Skill(*)",
                "mcp__*",
            ],
            "deny": [],
            "defaultMode": "bypassPermissions",
        },
        "skipDangerousModePermissionPrompt": True,
    }

    try:
        # Merge with any existing settings the user may have added
        existing: dict[str, Any] = {}
        if settings_file.is_file():
            existing = json.loads(settings_file.read_text(encoding="utf-8"))

        # Overlay our permissions but preserve other user keys
        merged = {**existing, **desired}
        # Preserve user allow rules that aren't already covered
        if "permissions" in existing:
            user_allow = existing["permissions"].get("allow", [])
            our_allow = list(desired["permissions"]["allow"])
            for rule in user_allow:
                if rule not in our_allow:
                    our_allow.append(rule)
            merged["permissions"] = {
                **existing.get("permissions", {}),
                **desired["permissions"],
                "allow": our_allow,
            }

        claude_dir.mkdir(exist_ok=True)
        settings_file.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        _log.debug("Wrote Claude Code bypass settings -> %s", settings_file)
    except OSError as exc:
        _log.debug("Provider settings sync failed (claude): %s", exc)
