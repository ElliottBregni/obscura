"""Translate Obscura MCP server configs into Codex CLI config overrides."""

from __future__ import annotations

import re
from typing import Any, cast


def mcp_servers_to_config_overrides(
    servers: list[dict[str, Any]],
) -> tuple[str, ...]:
    """Map Obscura's ``mcp_servers`` list to Codex ``-c`` override strings."""
    overrides: list[str] = []
    for server in servers:
        name = str(server.get("name") or "").strip()
        if not name:
            continue
        key = codex_config_key(name)

        url = server.get("url")
        if isinstance(url, str) and url:
            overrides.append(f"mcp_servers.{key}.url={toml_str(url)}")
            bearer = server.get("bearer_token_env_var") or server.get(
                "bearer_token_env",
            )
            if isinstance(bearer, str) and bearer:
                overrides.append(
                    f"mcp_servers.{key}.bearer_token_env_var={toml_str(bearer)}",
                )
            continue

        command = server.get("command")
        if isinstance(command, str) and command:
            overrides.append(f"mcp_servers.{key}.command={toml_str(command)}")
            raw_args: Any = server.get("args")
            if isinstance(raw_args, list) and raw_args:
                args: list[Any] = cast("list[Any]", raw_args)
                overrides.append(f"mcp_servers.{key}.args={toml_string_array(args)}")
            raw_env: Any = server.get("env")
            if isinstance(raw_env, dict) and raw_env:
                env_map: dict[str, Any] = cast("dict[str, Any]", raw_env)
                overrides.append(f"mcp_servers.{key}.env={toml_inline_table(env_map)}")
    return tuple(overrides)


def codex_config_key(name: str) -> str:
    """Sanitize a server name for use as a TOML dotted-path key."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def toml_str(value: str) -> str:
    """Serialize a Python string as a TOML basic string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def toml_string_array(items: list[Any]) -> str:
    """Serialize a Python list as a TOML array of strings."""
    return "[" + ", ".join(toml_str(str(x)) for x in items) + "]"


def toml_inline_table(mapping: dict[str, Any]) -> str:
    """Serialize a Python dict as a TOML inline table of string values."""
    pairs = [
        f"{codex_config_key(str(k))} = {toml_str(str(v))}" for k, v in mapping.items()
    ]
    return "{ " + ", ".join(pairs) + " }"
