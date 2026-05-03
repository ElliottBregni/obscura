"""obscura.tools.system.team_prompt — Read the team-level system prompt.

Looks for a ``team_prompt.md`` (or ``team_prompt.txt``) file in the active
Obscura home directory (``~/.obscura/`` by default).  Returns the raw text so
agents can inspect or inject shared team instructions at runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

from obscura.core.paths import resolve_obscura_home
from obscura.core.tools import tool

_TEAM_PROMPT_FILENAMES = ("team_prompt.md", "team_prompt.txt", "team_prompt")


def _locate_team_prompt(base_dir: Path) -> Path | None:
    """Return the first matching team prompt file under *base_dir*, or None."""
    for name in _TEAM_PROMPT_FILENAMES:
        candidate = base_dir / name
        if candidate.is_file():
            return candidate
    return None


@tool(
    "read_team_prompt",
    (
        "Read the shared team-level system prompt from the Obscura home directory "
        "(~/.obscura/team_prompt.md by default). "
        "Returns the prompt text so agents can inspect or surface team instructions."
    ),
    {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Explicit path to the team prompt file. "
                    "If omitted, the tool searches ~/.obscura/ for "
                    "team_prompt.md, team_prompt.txt, or team_prompt."
                ),
            },
        },
        "required": [],
    },
)
async def read_team_prompt(path: str = "") -> str:
    """Return the contents of the team prompt file as a JSON payload."""
    if path:
        target = Path(path).expanduser().resolve()
    else:
        target_or_none = _locate_team_prompt(resolve_obscura_home())
        if target_or_none is None:
            return json.dumps(
                {
                    "ok": False,
                    "error": "team_prompt_not_found",
                    "searched": str(resolve_obscura_home()),
                    "filenames_tried": list(_TEAM_PROMPT_FILENAMES),
                },
            )
        target = target_or_none

    if not target.exists():
        return json.dumps({"ok": False, "error": "path_not_found", "path": str(target)})
    if not target.is_file():
        return json.dumps({"ok": False, "error": "not_a_file", "path": str(target)})

    try:
        text = target.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return json.dumps({"ok": False, "error": "read_error", "detail": str(exc)})

    return json.dumps({"ok": True, "path": str(target), "text": text})
