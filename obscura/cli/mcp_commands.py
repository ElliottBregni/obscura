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
    write_catalog_config,
)
from obscura.integrations.mcp.config_loader import (
    DiscoveredMCPServer,
    discover_mcp_servers,
    select_servers_for_task,
)
from obscura.core.paths import resolve_obscura_mcp_dir


# ---------------------------------------------------------------------------
# Discovery Commands
# ---------------------------------------------------------------------------


def cmd_mcp_discover(args: list[str]) -> None:
    """Discover available MCP servers from catalog.
    
    Usage:
        /mcp discover [--limit N] [--search KEYWORD]
    """
    limit = 20
    search_term = None
    
    # Parse args
    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        elif args[i] == "--search" and i + 1 < len(args):
            search_term = args[i + 1].lower()
            i += 2
        else:
            i += 1
    
    console.print("\n🔍 [bold cyan]Discovering MCP Servers...[/bold cyan]\n")
    
    # Try community catalog (official registry is not working)
    try:
        provider = MCPServersOrgCatalogProvider()
        entries = provider.fetch_top(limit=limit)
        
        if search_term:
            entries = [
                e for e in entries 
                if search_term in e.name.lower() or search_term in e.slug.lower()
            ]
        
        if not entries:
            print_info(f"No servers found{f' matching \"{search_term}\"' if search_term else ''}")
            return
        
        # Display results in a table
        table = Table(title=f"Available MCP Servers (showing {len(entries)})")
        table.add_column("#", style="dim", width=4)
        table.add_column("Name", style="cyan")
        table.add_column("Slug", style="green")
        table.add_column("Install Command", style="yellow")
        
        for entry in entries[:limit]:
            table.add_row(
                str(entry.rank),
                entry.name[:50] + "..." if len(entry.name) > 50 else entry.name,
                entry.slug,
                f"npx -y {entry.slug}"
            )
        
        console.print(table)
        console.print(f"\n💡 Use [cyan]/mcp install <slug>[/cyan] to add a server to your config\n")
        
    except Exception as e:
        print_error(f"Discovery failed: {e}")


def cmd_mcp_list(args: list[str]) -> None:
    """List currently configured MCP servers.
    
    Usage:
        /mcp list [--check-env]
    """
    check_env = "--check-env" in args
    
    try:
        servers = discover_mcp_servers()
        
        if not servers:
            print_info("No MCP servers configured")
            print_info(f"Config location: {resolve_obscura_mcp_dir()}")
            return
        
        console.print(f"\n📦 [bold cyan]Configured MCP Servers ({len(servers)})[/bold cyan]\n")
        
        table = Table(show_header=True)
        table.add_column("Name", style="cyan", width=20)
        table.add_column("Transport", style="green", width=10)
        table.add_column("Command", style="yellow", width=30)
        table.add_column("Status", style="white", width=20)
        
        for server in servers:
            # Status check
            status_parts = []
            if server.missing_env:
                status_parts.append(f"⚠️  Missing: {', '.join(server.missing_env)}")
            else:
                status_parts.append("✅ Ready")
            
            cmd_display = f"{Path(server.command).name}"
            if server.args:
                cmd_display += f" {' '.join(server.args[:2])}"
                if len(server.args) > 2:
                    cmd_display += "..."
            
            table.add_row(
                server.name,
                server.transport.value,
                cmd_display,
                " ".join(status_parts)
            )
        
        console.print(table)
        
        # Show missing env vars summary
        missing_count = sum(1 for s in servers if s.missing_env)
        if missing_count > 0:
            console.print(f"\n⚠️  [yellow]{missing_count} server(s) need environment variables[/yellow]")
            console.print("💡 Use [cyan]/mcp env[/cyan] to set up missing variables\n")
        else:
            console.print(f"\n✅ [green]All servers ready to use![/green]\n")
            
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
        
        console.print(f"\n🎯 [bold cyan]Task:[/bold cyan] {task_text}\n")
        
        if selected:
            console.print(f"✅ [green]Selected {len(selected)} server(s):[/green]\n")
            
            table = Table(show_header=True)
            table.add_column("Server", style="cyan")
            table.add_column("Matched Keywords", style="yellow")
            table.add_column("Status", style="white")
            
            # Import keyword map
            from obscura.integrations.mcp.config_loader import _SERVER_KEYWORDS
            
            for name in selected:
                server = next((s for s in servers if s.name == name), None)
                if not server:
                    continue
                
                keywords = _SERVER_KEYWORDS.get(name, (name,))
                matched_kws = [kw for kw in keywords if kw.lower() in task_text.lower()]
                
                status = "⚠️  Missing env" if server.missing_env else "✅ Ready"
                
                table.add_row(
                    name,
                    ", ".join(matched_kws[:3]),
                    status
                )
            
            console.print(table)
            console.print(f"\n💡 These servers will be used for this task\n")
        else:
            console.print("ℹ️  [yellow]No specific servers matched[/yellow]")
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
        
        # Collect all missing env vars
        missing_map: dict[str, list[str]] = {}
        for server in servers:
            if server.missing_env:
                missing_map[server.name] = list(server.missing_env)
        
        if not missing_map:
            print_ok("✅ All environment variables are set!")
            return
        
        console.print(f"\n⚠️  [yellow]Missing Environment Variables[/yellow]\n")
        
        if export_format:
            console.print("[dim]# Add these to your ~/.zshrc or ~/.bashrc:[/dim]\n")
            all_vars = set()
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
                    
                    table.add_row(
                        server_name,
                        var,
                        display_value
                    )
            
            console.print(table)
            console.print(f"\n💡 Use [cyan]/mcp env --export[/cyan] to get export commands\n")
            
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
    
    # Parse optional --name
    if "--name" in args:
        idx = args.index("--name")
        if idx + 1 < len(args):
            name = args[idx + 1]
    
    if not name:
        name = slug.replace("-mcp", "").replace("-", "_")
    
    try:
        # Load existing config
        mcp_dir = resolve_obscura_mcp_dir()
        config_file = mcp_dir / "config.json"
        
        if config_file.exists():
            with open(config_file) as f:
                config = json.load(f)
        else:
            config = {"mcpServers": {}}
        
        # Check if already exists
        if name in config["mcpServers"]:
            print_error(f"Server '{name}' already exists in config")
            return
        
        # Add new server
        config["mcpServers"][name] = {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", slug],
            "env": {},
            "tools": []
        }
        
        # Write config
        mcp_dir.mkdir(parents=True, exist_ok=True)
        with open(config_file, "w") as f:
            json.dump(config, f, indent=2)
        
        print_ok(f"✅ Installed '{name}' → {slug}")
        print_info(f"Config: {config_file}")
        console.print(f"\n💡 Run [cyan]/mcp list[/cyan] to see all configured servers\n")
        
    except Exception as e:
        print_error(f"Installation failed: {e}")


# ---------------------------------------------------------------------------
# Command Registry
# ---------------------------------------------------------------------------

MCP_COMMANDS: dict[str, tuple[Callable[[list[str]], None], str]] = {
    "discover": (cmd_mcp_discover, "Discover available MCP servers from catalog"),
    "list": (cmd_mcp_list, "List currently configured MCP servers"),
    "select": (cmd_mcp_select, "Auto-select servers based on task keywords"),
    "env": (cmd_mcp_env, "Check and set up environment variables"),
    "install": (cmd_mcp_install, "Install a new MCP server to config"),
}


def handle_mcp_command(args: list[str]) -> None:
    """Route /mcp subcommands."""
    if not args:
        # Show help
        console.print("\n[bold cyan]MCP Server Management Commands:[/bold cyan]\n")
        table = Table(show_header=True, show_lines=False)
        table.add_column("Command", style="cyan", width=20)
        table.add_column("Description", style="white")
        
        for cmd, (_, desc) in MCP_COMMANDS.items():
            table.add_row(f"/mcp {cmd}", desc)
        
        console.print(table)
        console.print("\n💡 Use [cyan]/mcp <command> --help[/cyan] for detailed usage\n")
        return
    
    subcommand = args[0]
    subargs = args[1:]
    
    if subcommand in MCP_COMMANDS:
        handler, _ = MCP_COMMANDS[subcommand]
        handler(subargs)
    else:
        print_error(f"Unknown MCP command: {subcommand}")
        console.print("💡 Use [cyan]/mcp[/cyan] to see available commands\n")
