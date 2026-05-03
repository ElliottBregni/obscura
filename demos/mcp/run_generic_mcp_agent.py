from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def add_server(
    *,
    path: Path | str,
    name: str,
    transport: str,
    command: str,
    args: tuple[str, ...] | list[str] = (),
    url: str = "",
    env: dict[str, str] | None = None,
    tools: list[str] | None = None,
) -> None:
    p = Path(path)
    root: dict[str, Any] = {"mcpServers": {}}
    if p.exists():
        try:
            root = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(root, dict):
                root = {"mcpServers": {}}
        except Exception:
            root = {"mcpServers": {}}
    servers = root.setdefault("mcpServers", {})
    servers[name] = {
        "transport": transport,
        "command": command,
        "args": list(args),
        "url": url,
        "env": env or {},
        "tools": tools or [],
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(root, indent=2) + "\n", encoding="utf-8")
