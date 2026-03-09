"""Load project-level hooks from ``.obscura/settings.json`` and ``.obscura/hooks/``.

Three hook sources are supported (merged in priority order):

1. **settings.json** — Claude Code-style JSON with a ``hooks`` section::

       {
         "hooks": {
           "preToolUse": [
             { "bash": "my-linter --check", "matcher": "run_shell" }
           ]
         }
       }

2. **hooks/hooks.json** — manifest that maps script names to events::

       {
         "hooks": {
           "preToolUse": [
             { "name": "git-guardian", "script": "git-guardian.sh", "matcher": "Bash" }
           ]
         }
       }

3. **hooks/ directory** — filename convention fallback for scripts
   not listed in ``hooks.json``::

       .obscura/hooks/
       ├── pre-tool-use.sh              # wildcard
       ├── pre-tool-use--run_shell.sh   # matcher=run_shell
       └── post-tool-use.py

All sources are loaded at CLI startup and merged into a single
:class:`~obscura.core.hooks.HookRegistry`.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, cast

from obscura.core.hooks import HookRegistry
from obscura.core.paths import resolve_obscura_hooks_dir, resolve_obscura_settings
from obscura.manifest.models import HookDefinition

logger = logging.getLogger(__name__)

# Supported script extensions → interpreter command
_SCRIPT_INTERPRETERS: dict[str, str] = {
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "zsh",
    ".py": "python3",
}

# Filename stem → event name
_FILENAME_EVENT_MAP: dict[str, str] = {
    "pre-tool-use": "preToolUse",
    "post-tool-use": "postToolUse",
    "session-start": "sessionStart",
    "session-init": "sessionStart",
    "session-end": "sessionEnd",
    "session-stop": "sessionEnd",
    "error-occurred": "errorOccurred",
}


def load_settings_hooks(cwd: Path | None = None) -> list[HookDefinition]:
    """Load hook definitions from ``.obscura/settings.json``.

    Returns an empty list if the file doesn't exist or has no ``hooks`` key.
    """
    settings_path = resolve_obscura_settings(cwd)
    if not settings_path.is_file():
        return []

    try:
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not parse %s: %s", settings_path, exc)
        return []

    if not isinstance(raw, dict):
        return []

    raw_dict = cast("dict[str, Any]", raw)
    hooks_section = raw_dict.get("hooks")
    if not isinstance(hooks_section, dict):
        return []

    definitions: list[HookDefinition] = []
    hooks_dict = cast("dict[str, Any]", hooks_section)

    for event_name, entries in hooks_dict.items():
        if not isinstance(entries, list):
            logger.warning(
                "settings.json hooks[%s]: expected list, got %s",
                event_name,
                type(entries).__name__,
            )
            continue

        for entry in cast("list[Any]", entries):
            if not isinstance(entry, dict):
                continue
            entry_dict = cast("dict[str, Any]", entry)
            definitions.append(
                HookDefinition(
                    event=event_name,
                    type=str(entry_dict.get("type", "command")),
                    bash=str(entry_dict.get("bash", "")),
                    matcher=str(entry_dict.get("matcher", "")),
                    timeout_sec=int(entry_dict.get("timeout_sec", 10)),
                    comment=str(entry_dict.get("comment", "")),
                )
            )

    return definitions


def _load_hooks_json(hooks_dir: Path) -> list[HookDefinition]:
    """Load hook definitions from ``hooks.json`` inside the hooks directory.

    The manifest maps descriptive script names to lifecycle events::

        {
          "hooks": {
            "preToolUse": [
              {"name": "git-guardian", "script": "git-guardian.sh", "matcher": "Bash"}
            ]
          }
        }

    Returns an empty list if ``hooks.json`` doesn't exist or is invalid.
    Scripts that are missing or non-executable are skipped with a warning.
    """
    manifest_path = hooks_dir / "hooks.json"
    if not manifest_path.is_file():
        return []

    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not parse %s: %s", manifest_path, exc)
        return []

    if not isinstance(raw, dict):
        return []

    raw_dict = cast("dict[str, Any]", raw)
    hooks_section = raw_dict.get("hooks")
    if not isinstance(hooks_section, dict):
        return []

    definitions: list[HookDefinition] = []
    seen_scripts: set[str] = set()

    for event_name, entries in cast("dict[str, Any]", hooks_section).items():
        if not isinstance(entries, list):
            continue

        for entry in cast("list[Any]", entries):
            if not isinstance(entry, dict):
                continue
            entry_dict = cast("dict[str, Any]", entry)

            script_name = str(entry_dict.get("script", ""))
            if not script_name:
                continue

            script_path = hooks_dir / script_name
            if not script_path.is_file():
                logger.warning(
                    "hooks.json: script %s not found; skipping", script_name,
                )
                continue

            ext = script_path.suffix.lower()
            interpreter = _SCRIPT_INTERPRETERS.get(ext)
            if interpreter is None:
                logger.warning(
                    "hooks.json: script %s has unsupported extension; skipping",
                    script_name,
                )
                continue

            if not os.access(script_path, os.X_OK):
                logger.warning(
                    "hooks.json: script %s is not executable (chmod +x); skipping",
                    script_name,
                )
                continue

            bash_cmd = f"{interpreter} {script_path.resolve()}"
            matcher = str(entry_dict.get("matcher", ""))
            # Normalize wildcard matchers to empty string (hook system default)
            if matcher in (".*", "*"):
                matcher = ""
            hook_name = str(entry_dict.get("name", script_path.stem))

            definitions.append(
                HookDefinition(
                    event=event_name,
                    type="command",
                    bash=bash_cmd,
                    matcher=matcher,
                    comment=f"from hooks.json ({hook_name})",
                )
            )
            seen_scripts.add(script_name)

    return definitions


def load_directory_hooks(cwd: Path | None = None) -> list[HookDefinition]:
    """Load hook definitions from ``.obscura/hooks/``.

    Reads ``hooks.json`` first (authoritative manifest mapping script names
    to events), then falls back to the filename convention for any remaining
    executable scripts not covered by the manifest.

    Filename convention (fallback):
        - ``pre-tool-use.sh``            → event=preToolUse, no matcher
        - ``pre-tool-use--run_shell.sh`` → event=preToolUse, matcher=run_shell
    """
    hooks_dir = resolve_obscura_hooks_dir(cwd)
    if not hooks_dir.is_dir():
        return []

    # --- Phase 1: load from hooks.json manifest ---
    manifest_defs = _load_hooks_json(hooks_dir)
    # Track which scripts are already claimed by the manifest
    claimed_scripts: set[str] = set()
    for defn in manifest_defs:
        # Extract script filename from the bash command
        # Format: "interpreter /absolute/path/to/script.sh"
        parts = defn.bash.split(" ", 1)
        if len(parts) == 2:
            claimed_scripts.add(Path(parts[1]).name)

    # --- Phase 2: filename convention fallback for unclaimed scripts ---
    fallback_defs: list[HookDefinition] = []
    for path in sorted(hooks_dir.iterdir()):
        if not path.is_file():
            continue

        # Skip non-script files and already-claimed scripts
        if path.name in claimed_scripts:
            continue

        ext = path.suffix.lower()
        interpreter = _SCRIPT_INTERPRETERS.get(ext)
        if interpreter is None:
            continue

        if not os.access(path, os.X_OK):
            logger.warning(
                "Hook script %s is not executable (chmod +x); skipping",
                path,
            )
            continue

        # Parse filename: {event-prefix}[--{matcher}].{ext}
        stem = path.stem
        matcher = ""
        if "--" in stem:
            parts = stem.split("--", 1)
            stem = parts[0]
            matcher = parts[1]

        event_name = _FILENAME_EVENT_MAP.get(stem)
        if event_name is None:
            logger.debug(
                "Hook script %s: not in hooks.json and filename '%s' "
                "doesn't match a known event prefix; skipping",
                path.name,
                stem,
            )
            continue

        bash_cmd = f"{interpreter} {path.resolve()}"
        fallback_defs.append(
            HookDefinition(
                event=event_name,
                type="command",
                bash=bash_cmd,
                matcher=matcher,
                comment=f"from {path.name}",
            )
        )

    return manifest_defs + fallback_defs


def load_all_hooks(cwd: Path | None = None) -> HookRegistry:
    """Load hooks from both ``.obscura/settings.json`` and ``.obscura/hooks/``.

    Returns a merged :class:`HookRegistry`.  If neither source has hooks,
    the returned registry has ``count == 0``.
    """
    settings_defs = load_settings_hooks(cwd)
    dir_defs = load_directory_hooks(cwd)
    all_defs = settings_defs + dir_defs

    if not all_defs:
        return HookRegistry()

    registry = HookRegistry.from_hook_definitions(all_defs)
    logger.debug(
        "Loaded %d project hooks (%d from settings.json, %d from hooks/)",
        registry.count,
        len(settings_defs),
        len(dir_defs),
    )
    return registry


def list_hook_sources(cwd: Path | None = None) -> list[dict[str, Any]]:
    """List all discovered hook sources with metadata.

    Returns a list of dicts describing each hook, useful for the
    ``hook_tool`` to expose to the agent.
    """
    sources: list[dict[str, Any]] = []

    for defn in load_settings_hooks(cwd):
        sources.append({
            "source": "settings.json",
            "event": defn.event,
            "type": defn.type,
            "bash": defn.bash,
            "matcher": defn.matcher or "(all)",
            "timeout_sec": defn.timeout_sec,
            "comment": defn.comment,
        })

    for defn in load_directory_hooks(cwd):
        sources.append({
            "source": "hooks/",
            "event": defn.event,
            "type": defn.type,
            "bash": defn.bash,
            "matcher": defn.matcher or "(all)",
            "timeout_sec": defn.timeout_sec,
            "comment": defn.comment,
        })

    return sources
