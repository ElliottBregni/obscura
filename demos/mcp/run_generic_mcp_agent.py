"""Generic MCP demo: discover, add, and run agents with MCP servers."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from obscura.agent.agents import AgentRuntime, MCPConfig
from obscura.demo.framework import DemoAgentConfig, run_demo_prompt


@dataclass(frozen=True)
class DiscoveredMCPServer:
    name: str
    transport: str
    command: str
    args: tuple[str, ...]
    url: str
    env: dict[str, str]
    missing_env: tuple[str, ...]


def load_mcp_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"mcpServers": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("MCP config must be a JSON object")
    root = cast(dict[str, Any], data)
    if "mcpServers" not in root or not isinstance(root["mcpServers"], dict):
        root["mcpServers"] = {}
    return root


def save_mcp_config(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _resolve_env_value(raw: str) -> tuple[str, str | None]:
    if raw.startswith("${") and raw.endswith("}"):
        key = raw[2:-1].strip()
        value = os.environ.get(key, "")
        if not value:
            return "", key
        return value, None
    return raw, None


def discover_servers(path: Path) -> list[DiscoveredMCPServer]:
    config = load_mcp_config(path)
    mcp_servers = cast(dict[str, Any], config["mcpServers"])
    discovered: list[DiscoveredMCPServer] = []
    for name, raw in mcp_servers.items():
        if not isinstance(raw, dict):
            continue
        entry = cast(dict[str, Any], raw)
        transport = str(entry.get("transport", "stdio"))
        command = str(entry.get("command", ""))
        args_raw = entry.get("args", [])
        args: tuple[str, ...] = tuple(
            str(v) for v in args_raw if isinstance(args_raw, list)
        )
        url = str(entry.get("url", ""))
        env_map_raw = entry.get("env", {})
        env_map = cast(dict[str, Any], env_map_raw) if isinstance(env_map_raw, dict) else {}
        resolved_env: dict[str, str] = {}
        missing: list[str] = []
        for k, v in env_map.items():
            value, missing_key = _resolve_env_value(str(v))
            if missing_key:
                missing.append(missing_key)
            resolved_env[str(k)] = value
        discovered.append(
            DiscoveredMCPServer(
                name=str(name),
                transport=transport,
                command=command,
                args=args,
                url=url,
                env=resolved_env,
                missing_env=tuple(missing),
            )
        )
    return discovered


def add_server(
    *,
    path: Path,
    name: str,
    transport: str,
    command: str,
    args: tuple[str, ...],
    url: str,
    env: dict[str, str],
) -> None:
    config = load_mcp_config(path)
    mcp_servers = cast(dict[str, Any], config["mcpServers"])
    entry: dict[str, Any] = {"transport": transport, "env": env, "tools": []}
    if transport == "stdio":
        entry["command"] = command
        entry["args"] = list(args)
    else:
        entry["url"] = url
    mcp_servers[name] = entry
    save_mcp_config(path, config)


def build_agent_servers(
    discovered: list[DiscoveredMCPServer],
    selected_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for server in discovered:
        if selected_names and server.name not in selected_names:
            continue
        if server.transport == "stdio":
            result.append(
                {
                    "transport": "stdio",
                    "command": server.command,
                    "args": list(server.args),
                    "env": server.env,
                }
            )
        else:
            result.append(
                {
                    "transport": "sse",
                    "url": server.url,
                    "env": server.env,
                }
            )
    return result


async def run_mcp_agent(
    *,
    model: str,
    prompt: str,
    servers: list[dict[str, Any]],
    stream: bool,
    start_timeout_seconds: float,
    run_timeout_seconds: float,
) -> str:
    config = DemoAgentConfig(
        name="generic-mcp-demo",
        model=model,
        role=f"agent:{model}",
        system_prompt=(
            "You are a generic MCP demo agent. Use available tools when needed "
            "and return concise factual output."
        ),
        memory_namespace="demo:generic:mcp",
    )
    return await run_demo_prompt(
        config,
        prompt,
        stream=stream,
        runtime_cls=AgentRuntime,
        start_timeout_seconds=start_timeout_seconds,
        run_timeout_seconds=run_timeout_seconds,
        spawn_kwargs={"mcp": MCPConfig(enabled=True, servers=servers)},
    )


def _parse_json_map(raw: str) -> dict[str, str]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Expected JSON object")
    return {str(k): str(v) for k, v in cast(dict[str, Any], payload).items()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generic MCP demo CLI")
    parser.add_argument(
        "--config",
        default="config/mcp-config.json",
        help="Path to MCP config JSON file.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="List/discover MCP servers from config")
    discover.add_argument("--json", action="store_true")

    add = sub.add_parser("add", help="Add a server to MCP config")
    add.add_argument("--name", required=True)
    add.add_argument("--transport", choices=("stdio", "sse"), default="stdio")
    add.add_argument("--server-command", dest="server_command", default="")
    add.add_argument("--args", nargs="*", default=[])
    add.add_argument("--url", default="")
    add.add_argument("--env", default="{}")

    run = sub.add_parser("run", help="Run a real agent with MCP servers")
    run.add_argument("--model", default="claude")
    run.add_argument(
        "--prompt",
        "-p",
        default="List available tools and use one to complete a simple task.",
    )
    run.add_argument("--servers", default="", help="Comma list of server names to use.")
    run.add_argument("--all", action="store_true", help="Use all discovered servers.")
    run.add_argument("--stream", action="store_true")
    run.add_argument("--start-timeout", type=float, default=30.0)
    run.add_argument("--run-timeout", type=float, default=180.0)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config_path = Path(str(args.config))

    if args.command == "discover":
        servers = discover_servers(config_path)
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "name": s.name,
                            "transport": s.transport,
                            "command": s.command,
                            "args": list(s.args),
                            "url": s.url,
                            "missing_env": list(s.missing_env),
                        }
                        for s in servers
                    ],
                    indent=2,
                )
            )
            return
        for server in servers:
            missing = ", ".join(server.missing_env) if server.missing_env else "none"
            print(
                f"{server.name}: transport={server.transport} "
                f"missing_env={missing}"
            )
        return

    if args.command == "add":
        env_map = _parse_json_map(str(args.env))
        add_server(
            path=config_path,
            name=str(args.name),
            transport=str(args.transport),
            command=str(args.server_command),
            args=tuple(cast(list[str], args.args)),
            url=str(args.url),
            env=env_map,
        )
        print(f"Added MCP server '{args.name}' to {config_path}")
        return

    # run
    servers = discover_servers(config_path)
    if args.all:
        selected: set[str] | None = None
    else:
        selected = {s.strip() for s in str(args.servers).split(",") if s.strip()}
        if not selected:
            raise SystemExit("Specify --servers name1,name2 or pass --all")
    selected_servers = build_agent_servers(servers, selected)
    if not selected_servers:
        raise SystemExit("No MCP servers selected/found.")
    result = asyncio.run(
        run_mcp_agent(
            model=str(args.model),
            prompt=str(args.prompt),
            servers=selected_servers,
            stream=bool(args.stream),
            start_timeout_seconds=float(args.start_timeout),
            run_timeout_seconds=float(args.run_timeout),
        )
    )
    print(result)


if __name__ == "__main__":
    main()
