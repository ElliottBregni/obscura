"""Helpers to discover and normalize MCP server configs."""

from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from obscura.core.paths import resolve_obscura_mcp_dir
from obscura.integrations.mcp.types import MCPTransportType

_ENV_VAR_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


@dataclass(frozen=True)
class DiscoveredMCPServer:
    """Normalized MCP server loaded from a JSON config file."""

    name: str
    transport: MCPTransportType
    command: str
    args: tuple[str, ...]
    url: str
    env: dict[str, str]
    tools: tuple[str, ...]
    missing_env: tuple[str, ...]


def _resolve_env_value(raw: str, *, resolve_env: bool) -> tuple[str, str | None]:
    if not resolve_env:
        return raw, None
    match = _ENV_VAR_PATTERN.match(raw)
    if match is None:
        return raw, None
    key = match.group(1)
    value = os.environ.get(key, "")
    if value:
        return value, None
    return "", key


def _load_config_root(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"mcpServers": {}}
    if path.suffix.lower() == ".toml":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("MCP config must be a JSON object")
    root = cast(dict[str, Any], data)
    mcp_servers = root.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        root["mcpServers"] = {}
    return root


def _merge_roots(roots: Sequence[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {"mcpServers": {}}
    target = cast(dict[str, Any], merged["mcpServers"])
    for root in roots:
        raw_servers = root.get("mcpServers")
        if not isinstance(raw_servers, dict):
            continue
        servers_dict = cast(dict[str, Any], raw_servers)
        for name, raw in servers_dict.items():
            target[str(name)] = raw
    return merged


def _resolve_default_config_path() -> Path:
    mcp_dir = resolve_obscura_mcp_dir()
    if mcp_dir.is_dir():
        return mcp_dir
    legacy_file = Path("config/mcp-config.json").resolve()
    if legacy_file.exists():
        return legacy_file
    return mcp_dir


def _load_roots(path: Path) -> dict[str, Any]:
    expanded = path.expanduser().resolve()
    if expanded.is_dir():
        roots: list[dict[str, Any]] = []
        for config_file in sorted(expanded.iterdir()):
            if not config_file.is_file():
                continue
            if config_file.suffix.lower() not in (".json", ".toml"):
                continue
            roots.append(_load_config_root(config_file))
        return _merge_roots(roots)
    return _load_config_root(expanded)


def _tuple_of_str(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    values = cast(list[Any], value)
    return tuple(str(item) for item in values)


def _dict_of_any(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        return {}
    return cast(dict[str, Any], value)


def _resolve_command_binary(command: str) -> str:
    if not command:
        return ""
    if "/" in command:
        return command

    direct = shutil.which(command)
    if direct:
        return direct

    if command == "npx":
        nvm_root = Path.home() / ".nvm" / "versions" / "node"
        if nvm_root.is_dir():
            candidates = sorted(p for p in nvm_root.glob("*/bin/npx") if p.is_file())
            if candidates:
                return str(candidates[-1])

    return command


def discover_mcp_servers(
    config_path: str | Path | None = None,
    *,
    resolve_env: bool = True,
) -> list[DiscoveredMCPServer]:
    """Discover MCP servers declared in a config file."""
    path = _resolve_default_config_path() if config_path is None else Path(config_path)
    root = _load_roots(path)
    raw_servers = cast(dict[str, Any], root["mcpServers"])
    discovered: list[DiscoveredMCPServer] = []

    for raw_name, raw_entry in raw_servers.items():
        if not isinstance(raw_entry, dict):
            continue
        name = str(raw_name)
        entry = cast(dict[str, Any], raw_entry)

        raw_transport = str(entry.get("transport", "stdio")).lower()
        if raw_transport == "stdio":
            transport = MCPTransportType.STDIO
        elif raw_transport == "sse":
            transport = MCPTransportType.SSE
        else:
            raise ValueError(
                f"Unsupported MCP transport '{raw_transport}' for '{name}'"
            )

        args = _tuple_of_str(entry.get("args", []))
        tools = _tuple_of_str(entry.get("tools", []))

        env_map = _dict_of_any(entry.get("env", {}))
        resolved_env: dict[str, str] = {}
        missing_env: list[str] = []
        for key, raw_value in env_map.items():
            value, missing_key = _resolve_env_value(
                str(raw_value), resolve_env=resolve_env
            )
            resolved_env[key] = value
            if missing_key is not None:
                missing_env.append(missing_key)

        discovered.append(
            DiscoveredMCPServer(
                name=name,
                transport=transport,
                command=_resolve_command_binary(str(entry.get("command", ""))),
                args=args,
                url=str(entry.get("url", "")),
                env=resolved_env,
                tools=tools,
                missing_env=tuple(missing_env),
            )
        )

    return discovered


def build_runtime_server_configs(
    discovered: Sequence[DiscoveredMCPServer],
    selected_names: Sequence[str] | None = None,
    primary_server_name: str = "github",
) -> list[dict[str, Any]]:
    """Convert discovered MCP servers into agent runtime server dicts."""
    selected: set[str] | None = (
        set(selected_names) if selected_names is not None else None
    )
    available_names = {server.name for server in discovered}
    if selected is not None:
        missing = sorted(selected.difference(available_names))
        if missing:
            missing_text = ", ".join(missing)
            raise ValueError(f"Unknown MCP server(s): {missing_text}")

    ordered_servers: list[DiscoveredMCPServer]
    if selected_names is not None:
        by_name: dict[str, DiscoveredMCPServer] = {
            server.name: server for server in discovered
        }
        ordered_servers = [by_name[name] for name in selected_names]
    else:
        ordered_servers = sorted(
            discovered,
            key=lambda server: (
                0 if server.name == primary_server_name else 1,
                server.name,
            ),
        )

    runtime_servers: list[dict[str, Any]] = []
    for server in ordered_servers:
        payload: dict[str, Any] = {
            "transport": server.transport.value,
            "env": dict(server.env),
            "tools": list(server.tools),
        }
        if server.transport is MCPTransportType.STDIO:
            payload["command"] = server.command
            payload["args"] = list(server.args)
        else:
            payload["url"] = server.url
        runtime_servers.append(payload)

    return runtime_servers
