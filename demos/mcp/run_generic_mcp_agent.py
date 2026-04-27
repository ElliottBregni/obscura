# Compatibility shim that provides add_server used by integration tests
from __future__ import annotations
from pathlib import Path
from typing import Any
import json


def add_server(path: Path | str, **kwargs: Any) -> None:
    """Add a server entry to the given config path.

    Attempt to delegate to the core config loader when available. Import is
    performed lazily to avoid raising ImportError at module import time when
    the core helper is not present.
    """
    try:
        from obscura.integrations.mcp.config_loader import add_server as core_add_server
    except Exception:
        core_add_server = None  # type: ignore

    if core_add_server is not None:
        try:
            core_add_server(path=path, **kwargs)
            return
        except Exception:
            pass

    # Fallback: merge into existing JSON config if present, otherwise create.
    p = Path(path)
    existing: dict[str, Any] = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    servers = existing.get("mcpServers") if isinstance(existing.get("mcpServers"), dict) else {}
    servers = dict(servers)  # copy
    servers[str(kwargs.get("name"))] = kwargs
    existing["mcpServers"] = servers

    p.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
