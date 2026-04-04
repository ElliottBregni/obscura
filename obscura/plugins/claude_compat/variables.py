"""obscura.plugins.claude_compat.variables — Variable substitution for Claude Code plugins.

Handles ``${CLAUDE_PLUGIN_ROOT}``, ``${CLAUDE_PLUGIN_DATA}``,
``${CLAUDE_SKILL_DIR}``, ``${CLAUDE_SESSION_ID}``, and
``${user_config.KEY}`` expansion in plugin configs and content.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Pattern matches ${VAR_NAME} and ${user_config.KEY}.
_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def substitute_variables(
    text: str,
    *,
    plugin_root: Path,
    plugin_data: Path,
    skill_dir: Path | None = None,
    session_id: str = "",
    user_config: dict[str, str] | None = None,
) -> str:
    """Expand Claude Code plugin variables in *text*.

    Parameters
    ----------
    text:
        String possibly containing ``${...}`` references.
    plugin_root:
        Absolute path to the plugin's install directory.
    plugin_data:
        Absolute path to the plugin's persistent data directory.
    skill_dir:
        Absolute path to a specific skill's subdirectory (skills only).
    session_id:
        Current session ID (may be empty).
    user_config:
        Dict of user-provided config values for this plugin.

    Returns
    -------
    str
        Text with all recognized variables expanded. Unrecognized
        variables are left as-is (passthrough to shell/env expansion).
    """
    config = user_config or {}

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)

        # Built-in variables.
        if key == "CLAUDE_PLUGIN_ROOT":
            return str(plugin_root)
        if key == "CLAUDE_PLUGIN_DATA":
            return str(plugin_data)
        if key == "CLAUDE_SKILL_DIR" and skill_dir is not None:
            return str(skill_dir)
        if key == "CLAUDE_SESSION_ID":
            return session_id

        # User config references: ${user_config.api_key}
        if key.startswith("user_config."):
            config_key = key[len("user_config.") :]
            return config.get(config_key, match.group(0))

        # Fall through to env vars.
        env_val = os.environ.get(key)
        if env_val is not None:
            return env_val

        # Leave unresolved references intact.
        return match.group(0)

    return _VAR_PATTERN.sub(_replace, text)


def get_plugin_data_dir(plugin_id: str) -> Path:
    """Return the persistent data directory for a Claude Code plugin.

    Creates the directory if it doesn't exist.  Maps to
    ``~/.obscura/plugins/claude_data/<sanitized-id>/``.
    """
    sanitized = re.sub(r"[^\w-]", "_", plugin_id)
    data_dir = Path.home() / ".obscura" / "plugins" / "claude_data" / sanitized
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir
