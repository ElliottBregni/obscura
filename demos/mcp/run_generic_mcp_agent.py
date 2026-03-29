"""Demo helper used by tests to write minimal MCP config files.

Tests expect add_server(...) to append or create a JSON config file at the
given path. Provide a small, well-scoped implementation that merges an
entry into the file under the top-level "mcpServers" key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def add_server(*, path: str | Path, name: str, transport: str, command: str, args: tuple[str, ...], url: str, env: dict[str, str]) -> None:
    p = Path(path)
    root: dict[str, Any] = {"mcpServers": {}}
    if p.exists():
        try:
            root = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            root = {"mcpServers": {}}

    mcp = root.setdefault("mcpServers", {})
    mcp[name] = {
        "transport": transport,
        "command": command,
        "args": list(args),
        "url": url,
        "env": env,
        "tools": [],
    }

    p.write_text(json.dumps(root, indent=2) + "\n", encoding="utf-8")
