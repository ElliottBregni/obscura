"""Auto-classify conversation turns into memory channel namespaces.

When :func:`~obscura.cli.vector_memory_bridge.auto_save_turn` saves a turn,
the classifier determines which channel namespaces the turn should be stored
in based on keyword and file-path triggers.
"""

from __future__ import annotations

import fnmatch
import re

from obscura.memory_channels.models import MemoryChannel

# Same regex as router.py
_FILE_PATH_RE = re.compile(r"[\w/.~-]+\.(?:py|toml|yaml|yml|json|md|ts|tsx|js|jsx|rs|go|sh)")

# Default namespace when no channels match
_DEFAULT_NAMESPACE = "cli:conversation"


class TurnClassifier:
    """Classify conversation turns into channel namespaces.

    Parameters
    ----------
    channels:
        List of :class:`MemoryChannel` definitions to match against.
    """

    def __init__(self, channels: list[MemoryChannel]) -> None:
        self._channels = [c for c in channels if c.enabled]

    def classify(self, user_text: str, assistant_text: str) -> list[str]:
        """Return namespace strings that this turn should be saved to.

        Always includes :data:`_DEFAULT_NAMESPACE`.  Additional namespaces
        are added when keyword or file-glob triggers match.
        """
        combined = f"{user_text}\n{assistant_text}".lower()
        file_paths = set(_FILE_PATH_RE.findall(combined))

        namespaces: list[str] = [_DEFAULT_NAMESPACE]

        for channel in self._channels:
            triggers = channel.triggers

            # Always-on channels get every turn
            if triggers.always:
                if channel.namespace not in namespaces:
                    namespaces.append(channel.namespace)
                continue

            # Keyword match
            if triggers.keywords:
                for kw in triggers.keywords:
                    if kw.lower() in combined:
                        if channel.namespace not in namespaces:
                            namespaces.append(channel.namespace)
                        break

            # File glob match
            if triggers.file_globs and channel.namespace not in namespaces:
                for glob_pattern in triggers.file_globs:
                    matched = any(fnmatch.fnmatch(fp, glob_pattern) for fp in file_paths)
                    if matched:
                        if channel.namespace not in namespaces:
                            namespaces.append(channel.namespace)
                        break

            # Tool name match (check if tool names appear in text)
            if triggers.tool_names and channel.namespace not in namespaces:
                for tn in triggers.tool_names:
                    if tn.lower() in combined:
                        if channel.namespace not in namespaces:
                            namespaces.append(channel.namespace)
                        break

        return namespaces
