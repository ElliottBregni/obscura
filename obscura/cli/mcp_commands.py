"""obscura.cli.mcp_commands — MCP server discovery, selection, and management."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

from rich.table import Table

from obscura.cli.render import console, print_error, print_info, print_ok
from obscura.integrations.mcp.catalog import (
    MCPCatalogEntry,
    MCPRegistryAPICatalogProvider,
    MCPServersOrgCatalogProvider,
    MCPSoCatalogProvider,
    REGISTRY_ALIASES,
    get_provider_for_registry,
    write_catalog_config,
)
from obscura.integrations.mcp.config_loader import (
    DiscoveredMCPServer,
    discover_mcp_servers,
    select_servers_for_task,
)
from obscura.core.paths import resolve_obscura_mcp_dir


_DEFAULT_REGISTRY = "mcp.so"


# ---------------------------------------------------------------------------
# Discovery Commands
# ---------------------------------------------------------------------------


def cmd_mcp_discover(args: list[str]) -> None:
    """Discover available MCP servers from a catalog registry.

    Usage:
        /mcp discover [--limit N] [--page N] [--search KEYWORD] [--registry NAME]

    Registries:
        mcp.so          Default (largest catalog)
        mcpservers.org  Community list
        official        registry.modelcontextprotocol.io
        <url>           Any custom base URL

    Examples:
        /mcp discover
        /mcp discover --limit 50
        /mcp discover --page 2
        /mcp discover --search github
        /mcp discover --registry mcpservers.org --limit 30
    """
    limit = 20
    page = 1
    search_term = None
    registry = _DEFAULT_REGISTRY

    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            try:
                limit = int(args[i + 1])
            except ValueError:
                print_error(f"Invalid --limit value: {args[i+1]}")
                return
            i += 2
        elif args[i] == "--page" and i + 1 < len(args):
            try:
                page = int(args[i + 1])
            except ValueError:
                print_error(f"Invalid --page value: {args[i+1]}")
                return
            i += 2
        elif args[i] == "--search" and i + 1 < len(args):
            search_term = args[i + 1].lower()
            i += 2
        elif args[i] == "--registry" and i + 1 < len(args):
            registry = args[i + 1]
            i += 2
        else:
            i += 1

    provider = get_provider_for_registry(registry)
    console.print(
        f"\n\U0001f50d [bold cyan]MCP Servers[/bold cyan] "
        f"[dim]via {registry} \u2014 page {page}, limit {limit}[/dim]\n"
    )

    try:
        if isinstance(provider, MCPSoCatalogProvider):
            entries = provider.fetch_top(limit=limit, page=page)
        else:
            # Providers without native page support: fetch enough and slice
            offset = (page - 1) * limit
            entries = provider.fetch_top(limit=offset + limit)
            entries = entries[offset:]

        if search_term:
            entries = [
                e for e in entries
                if search_term in e.name.lower() or search_term in e.slug.lower()
            ]

        if not entries:
            print_info(
                f"No servers found"
                f"{f' matching \"{search_term}\"' if search_term else ''}"
            )
            return

        table = Table(title=f"[{registry}] page {page} \u2014 {len(entries)} results")
        table.add_column("#", style="dim", width=5)
        table.add_column("Name", style="cyan")
        table.add_column("Slug", style="green")
        table.add_column("URL", style="dim")

        for entry in entries:
            table.add_row(
                str(entry.rank),
                entry.name[:48] + "\u2026" if len(entry.name) > 48 else entry.name,
                entry.slug,
                entry.url[:52] + "\u2026" if len(entry.url) > 52 else entry.url,
            )

        console.print(table)

        next_pg = page + 1
        console.print(
            f"\n\U0001f4a1 [dim]Next:[/dim] [cyan]/mcp discover --page {next_pg}[/cyan]  "
            f"[dim]More:[/dim] [cyan]/mcp discover --limit {limit * 2}[/cyan]  "
            f"[dim]Install:[/dim] [cyan]/mcp install <slug>[/cyan]\n"
        )
        registry_list = ", ".join(sorted(set(REGISTRY_ALIASES.keys())))
        console.print(f"[dim]Registries: {registry_list}[/dim]\n")

    except Exception as e:
        print_error(f"Discovery failed: {e}")
        console.print("[dim]Try: /mcp discover --registry mcpservers.org[/dim]\n")


def cmd_mcp_list(args: list[str]) -> None:
    """List currently configured MCP servers.

    Usage:
        /mcp list [--check-env]
    """
    try:
        servers = discover_mcp_servers()

        if not servers:
            print_info("No MCP servers configured")
            print_info(f"Config location: {resolve_obscura_mcp_dir()}")
            return

        console.print(f"\n\U0001f4e6 [bold cyan]Configured MCP Servers ({len(servers)})[/bold cyan]\n")

        table = Table(show_header=True)
        table.add_column("Name", style="cyan", width=20)
        table.add_column("Transport", style="green", width=10)
        table.add_column("Command", style="yellow", width=30)
        table.add_column("Status", style="white", width=20)

        for server in servers:
            status_parts = []
            if server.missing_env:
                status_parts.append(f"\u26a0\ufe0f  Missing: {', '.join(server.missing_env)}")
            else:
                status_parts.append("\u2705 Ready")

            cmd_display = f"{Path(server.command).name}"
            if server.args:
                cmd_display += f" {' '.join(server.args[:2])}"
                if len(server.args) > 2:
                    cmd_display += "..."

            table.add_row(
                server.name,
                server.transport.value,
                cmd_display,
                " ".join(status_parts),
            )

        console.print(table)

        missing_count = sum(1 for s in servers if s.missing_env)
        if missing_count > 0:
            console.print(f"\n\u26a0\ufe0f  [yellow]{missing_count} server(s) need environment variables[/yellow]")
            console.print("\U0001f4a1 Use [cyan]/mcp env[/cyan] to set up missing variables\n")
        else:
            console.print(f"\n\u2705 [green]All servers ready to use![/green]\n")

    except Exception as e:
        print_error(f"Failed to list servers: {e}")


def cmd_mcp_select(args: list[str]) -> None:
    """Auto-select MCP servers based on task keywords.

    Usage:
        /mcp select <task description>

    Examples:
        /mcp select create a github PR
        /mcp select query postgres database
        /mcp select scrape website with playwright
    """
    if not args:
        print_error("Usage: /mcp select <task description>")
        return

    task_text = " ".join(args)

    try:
        servers = discover_mcp_servers()
        selected = select_servers_for_task(servers, task_text)

        console.print(f"\n\U0001f3af [bold cyan]Task:[/bold cyan] {task_text}\n")

        if selected:
            console.print(f"\u2705 [green]Selected {len(selected)} server(s):[/green]\n")

            table = Table(show_header=True)
            table.add_column("Server", style="cyan")
            table.add_column("Matched Keywords", style="yellow")
            table.add_column("Status", style="white")

            from obscura.integrations.mcp.config_loader import _SERVER_KEYWORDS

            for name in selected:
                server = next((s for s in servers if s.name == name), None)
                if not server:
                    continue

                keywords = _SERVER_KEYWORDS.get(name, (name,))
                matched_kws = [kw for kw in keywords if kw.lower() in task_text.lower()]
                status = "\u26a0\ufe0f  Missing env" if server.missing_env else "\u2705 Ready"

                table.add_row(name, ", ".join(matched_kws[:3]), status)

            console.print(table)
            console.print(f"\n\U0001f4a1 These servers will be used for this task\n")
        else:
            console.print("\u2139\ufe0f  [yellow]No specific servers matched[/yellow]")
            console.print("   Using all available servers\n")

    except Exception as e:
        print_error(f"Selection failed: {e}")


def cmd_mcp_env(args: list[str]) -> None:
    """Check and help set up environment variables for MCP servers.

    Usage:
        /mcp env [--show] [--export]
    """
    show_values = "--show" in args
    export_format = "--export" in args

    try:
        servers = discover_mcp_servers()

        missing_map: dict[str, list[str]] = {}
        for server in servers:
            if server.missing_env:
                missing_map[server.name] = list(server.missing_env)

        if not missing_map:
            print_ok("\u2705 All environment variables are set!")
            return

        console.print(f"\n\u26a0\ufe0f  [yellow]Missing Environment Variables[/yellow]\n")

        if export_format:
            console.print("[dim]# Add these to your ~/.zshrc or ~/.bashrc:[/dim]\n")
            all_vars: set[str] = set()
            for vars_list in missing_map.values():
                all_vars.update(vars_list)
            for var in sorted(all_vars):
                console.print(f'export {var}="your_value_here"')
            console.print()
        else:
            table = Table(show_header=True)
            table.add_column("Server", style="cyan", width=20)
            table.add_column("Missing Variables", style="yellow", width=40)
            table.add_column("Current Value", style="dim", width=20)

            for server_name, vars_list in missing_map.items():
                for var in vars_list:
                    current = os.environ.get(var, "")
                    display_value = current if show_values and current else "<not set>"
                    table.add_row(server_name, var, display_value)

            console.print(table)
            console.print(f"\n\U0001f4a1 Use [cyan]/mcp env --export[/cyan] to get export commands\n")

    except Exception as e:
        print_error(f"Failed to check environment: {e}")


def cmd_mcp_install(args: list[str]) -> None:
    """Install a new MCP server to your config.

    Usage:
        /mcp install <slug> [--name NAME]

    Examples:
        /mcp install github-mcp
        /mcp install playwright-mcp --name browser
    """
    if not args:
        print_error("Usage: /mcp install <slug>")
        return

    slug = args[0]
    name = None

    if "--name" in args:
        idx = args.index("--name")
        if idx + 1 < len(args):
            name = args[idx + 1]

    if not name:
        name = slug.replace("-mcp", "").replace("-", "_")

    try:
        mcp_dir = resolve_obscura_mcp_dir()
        config_file = mcp_dir / "config.json"

        if config_file.exists():
            with open(config_file) as f:
                config = json.load(f)
        else:
            config = {"mcpServers": {}}

        if name in config["mcpServers"]:
            print_error(f"Server '{name}' already exists in config")
            return

        config["mcpServers"][name] = {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", slug],
            "env": {},
            "tools": [],
        }

        mcp_dir.mkdir(parents=True, exist_ok=True)
        with open(config_file, "w") as f:
            json.dump(config, f, indent=2)

        print_ok(f"\u2705 Installed '{name}' \u2192 {slug}")
        print_info(f"Config: {config_file}")
        console.print(f"\n\U0001f4a1 Run [cyan]/mcp list[/cyan] to see all configured servers\n")

    except Exception as e:
        print_error(f"Installation failed: {e}")


# ---------------------------------------------------------------------------
# Command Registry
# ---------------------------------------------------------------------------

MCP_COMMANDS: dict[str, tuple[Callable[[list[str]], None], str]] = {
    "discover": (cmd_mcp_discover, "Discover MCP servers from a registry (default: mcp.so)"),
    "list": (cmd_mcp_list, "List currently configured MCP servers"),
    "select": (cmd_mcp_select, "Auto-select servers based on task keywords"),
    "env": (cmd_mcp_env, "Check and set up environment variables"),
    "install": (cmd_mcp_install, "Install a new MCP server to config"),
}


def handle_mcp_command(args: list[str]) -> None:
    """Route /mcp subcommands."""
    if not args:
        console.print("\n[bold cyan]MCP Server Management Commands:[/bold cyan]\n")
        table = Table(show_header=True, show_lines=False)
        table.add_column("Command", style="cyan", width=25)
        table.add_column("Description", style="white")

        for cmd, (_, desc) in MCP_COMMANDS.items():
            table.add_row(f"/mcp {cmd}", desc)

        console.print(table)
        console.print(
            f"\n[dim]Default registry:[/dim] [cyan]mcp.so[/cyan]  "
            f"[dim]|  Switch:[/dim] [cyan]/mcp discover --registry mcpservers.org[/cyan]"
        )
        console.print("\U0001f4a1 Use [cyan]/mcp discover --page N[/cyan] for pagination\n")
        return

    subcommand = args[0]
    subargs = args[1:]

    if subcommand in MCP_COMMANDS:
        handler, _ = MCP_COMMANDS[subcommand]
        handler(subargs)
    else:
        print_error(f"Unknown MCP command: {subcommand}")
        console.print("\U0001f4a1 Use [cyan]/mcp[/cyan] to see available commands\n")
