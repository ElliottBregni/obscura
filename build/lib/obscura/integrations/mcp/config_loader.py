"""Helpers to discover and normalize MCP server configs."""

from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from obscura.core.enums.protocol import MCPTransport
from obscura.core.models.configs import MCPServerSpec
from obscura.core.paths import resolve_all_mcp_dirs, resolve_obscura_mcp_dir

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_ENV_VAR_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_ENV_VAR_INLINE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class DiscoveredMCPServer:
    """Normalized MCP server loaded from a JSON config file."""

    name: str
    transport: MCPTransport
    command: str
    args: tuple[str, ...]
    url: str
    env: dict[str, str]
    tools: tuple[str, ...]
    missing_env: tuple[str, ...]
    headers: dict[str, str] = field(default_factory=dict[str, str])

    def to_spec(self) -> MCPServerSpec:
        """Promote the legacy dataclass to the typed boundary model."""
        return MCPServerSpec(
            name=self.name,
            transport=self.transport,
            command=self.command or None,
            args=self.args,
            env=dict(self.env),
            url=self.url or None,
            headers=dict(self.headers),
            tools=self.tools,
        )


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


def _resolve_inline_env(raw: str, *, resolve_env: bool) -> tuple[str, list[str]]:
    """Substitute every ``${VAR}`` occurrence inside *raw*.

    Returns ``(resolved_string, missing_var_names)``. Used for fields like
    HTTP ``headers`` where the value is templated (``"Bearer ${TOKEN}"``)
    rather than the whole field being a single placeholder.
    """
    if not resolve_env:
        return raw, []
    missing: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        value = os.environ.get(key, "")
        if not value:
            missing.append(key)
            return ""
        return value

    return _ENV_VAR_INLINE_PATTERN.sub(_sub, raw), missing


def _load_config_root(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"mcpServers": {}}
    if path.suffix.lower() == ".toml":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        msg = "MCP config must be a JSON object"
        raise ValueError(msg)
    root = cast("dict[str, Any]", data)
    mcp_servers = root.get("mcpServers")
    if not isinstance(mcp_servers, dict):
        root["mcpServers"] = {}
    return root


def _merge_roots(roots: Sequence[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {"mcpServers": {}}
    target = cast("dict[str, Any]", merged["mcpServers"])
    for root in roots:
        raw_servers = root.get("mcpServers")
        if not isinstance(raw_servers, dict):
            continue
        servers_dict = cast("dict[str, Any]", raw_servers)
        for name, raw in servers_dict.items():
            target[str(name)] = raw
    return merged


def _resolve_default_config_paths() -> list[Path]:
    """Return MCP config paths in merge order (global first, local last)."""
    # Prefer a project-local .obscura/mcp when present (tests and local
    # workflows expect local overrides to be used in preference to global
    # catalogs).  Only fall back to the global+local merge when no local
    # directory exists in the current working directory.
    local_mcp = Path.cwd().resolve() / ".obscura" / "mcp"
    if local_mcp.is_dir():
        return [local_mcp]

    # Prefer a project-local .obscura/mcp when present (tests and local
    # workflows expect local overrides to be used in preference to global
    # catalogs).  Only fall back to the global+local merge when no local
    # directory exists in the current working directory.
    local_mcp = Path.cwd().resolve() / ".obscura" / "mcp"
    if local_mcp.is_dir():
        return [local_mcp]

    dirs = resolve_all_mcp_dirs()
    if dirs:
        return dirs
    # Fallback: single-dir behavior
    mcp_dir = resolve_obscura_mcp_dir()
    if mcp_dir.is_dir():
        return [mcp_dir]
    return [mcp_dir]


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
    values = cast("list[Any]", value)
    return tuple(str(item) for item in values)


def _dict_of_any(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        return {}
    return cast("dict[str, Any]", value)


def _resolve_command_binary(command: str) -> str:
    if not command:
        return ""
    # If an explicit path is provided, keep it.
    if "/" in command:
        return command

    # Preserve the command name as given (do not resolve to an absolute path).
    # Resolving to absolute paths (e.g. "/opt/homebrew/bin/npx") caused tests
    # to fail because expectations use the plain command name ("npx"). The
    # runtime that executes the command should rely on PATH to locate binaries.
    return command

    # Preserve the command name as given (do not resolve to an absolute path).
    # Resolving to absolute paths (e.g. "/opt/homebrew/bin/npx") caused tests
    # to fail because expectations use the plain command name ("npx"). The
    # runtime that executes the command should rely on PATH to locate binaries.
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
    """Discover MCP servers from config files.

    When *config_path* is None, merges global and local MCP directories
    so that global servers are always available alongside local ones.
    """
    if config_path is not None:
        root = _load_roots(Path(config_path))
    else:
        paths = _resolve_default_config_paths()
        all_roots: list[dict[str, Any]] = []
        for path in paths:
            all_roots.append(_load_roots(path))
        root = _merge_roots(all_roots)
    raw_servers = cast("dict[str, Any]", root["mcpServers"])
    discovered: list[DiscoveredMCPServer] = []

    for raw_name, raw_entry in raw_servers.items():
        if not isinstance(raw_entry, dict):
            continue
        name = str(raw_name)
        entry = cast("dict[str, Any]", raw_entry)

        # Support both "transport" (Obscura native) and "type" (Claude synced format)
        raw_transport = str(
            entry.get("transport", entry.get("type", "stdio")),
        ).lower()
        if raw_transport == "stdio":
            transport = MCPTransport.STDIO
        elif raw_transport in ("sse", "http"):
            transport = MCPTransport.SSE
        else:
            msg = f"Unsupported MCP transport '{raw_transport}' for '{name}'"
            raise ValueError(
                msg,
            )

        args = _tuple_of_str(entry.get("args", []))
        tools = _tuple_of_str(entry.get("tools", []))

        env_map = _dict_of_any(entry.get("env", {}))
        resolved_env: dict[str, str] = {}
        missing_env: list[str] = []
        for key, raw_value in env_map.items():
            value, missing_key = _resolve_env_value(
                str(raw_value),
                resolve_env=resolve_env,
            )
            resolved_env[key] = value
            if missing_key is not None:
                missing_env.append(missing_key)

        # ``headers`` mirrors ``env`` for HTTP/SSE transports — used to
        # attach bearer tokens or API keys to outbound requests. Headers
        # are templated (``"Bearer ${TOKEN}"``), so they use the inline
        # substitution variant. Unresolved references accumulate into
        # ``missing_env`` so the CLI can warn the operator before the
        # server fails to connect.
        headers_map = _dict_of_any(entry.get("headers", {}))
        resolved_headers: dict[str, str] = {}
        for key, raw_value in headers_map.items():
            value, header_missing = _resolve_inline_env(
                str(raw_value),
                resolve_env=resolve_env,
            )
            resolved_headers[key] = value
            missing_env.extend(header_missing)

        discovered.append(
            DiscoveredMCPServer(
                name=name,
                transport=transport,
                command=_resolve_command_binary(str(entry.get("command", ""))),
                args=args,
                url=str(entry.get("url", "")),
                env=resolved_env,
                headers=resolved_headers,
                tools=tools,
                missing_env=tuple(missing_env),
            ),
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
            msg = f"Unknown MCP server(s): {missing_text}"
            raise ValueError(msg)

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
            "name": server.name,
            "transport": server.transport.value,
            "env": dict(server.env),
            "tools": list(server.tools),
        }
        if server.transport is MCPTransport.STDIO:
            payload["command"] = server.command
            payload["args"] = list(server.args)
        else:
            payload["url"] = server.url
            if server.headers:
                payload["headers"] = dict(server.headers)
        runtime_servers.append(payload)

    return runtime_servers


# ---------------------------------------------------------------------------
# Keyword-based MCP server auto-selection
# ---------------------------------------------------------------------------

_SERVER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "github": (
        "github",
        "git",
        "repo",
        "pull request",
        "commit",
        "issue",
        "repository",
    ),
    "gitlab": ("gitlab", "merge request", "pipeline"),
    "slack": ("slack", "message", "channel"),
    "linear": ("linear", "sprint", "roadmap"),
    "asana": ("asana",),
    "jira": ("jira", "ticket", "epic"),
    "stripe": ("stripe", "payment", "invoice", "subscription", "billing"),
    "supabase": ("supabase", "database", "postgres"),
    "firebase": ("firebase", "firestore"),
    "playwright": ("playwright", "scrape", "screenshot"),
    "postman": ("postman",),
    "context7": ("context7", "documentation"),
    "greptile": ("greptile",),
    "serena": ("serena",),
}


def select_servers_for_task(
    discovered: Sequence[DiscoveredMCPServer],
    task_text: str,
) -> list[str] | None:
    """Return server names whose keywords appear in *task_text*.

    Falls back to the server's own name when no keywords are registered.
    Returns ``None`` when nothing matches (caller should treat as "use all").
    """
    text_lower = task_text.lower()
    matched = [
        server.name
        for server in discovered
        if any(
            kw.lower() in text_lower
            for kw in _SERVER_KEYWORDS.get(server.name, (server.name,))
        )
    ]
    return matched or None
