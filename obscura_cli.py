#!/usr/bin/env python3
"""
Obscura CLI — Command-line interface for the Obscura SDK.

Usage:
    obscura agent spawn --name reviewer --model claude
    obscura agent run <agent-id> --prompt "Review this code"
    obscura agent list
    obscura memory set <key> <value> --namespace session
    obscura memory get <key>
    obscura memory search <query>
    obscura serve --port 8080

Environment:
    OBSCURA_URL         API base URL (default: http://localhost:8080)
    OBSCURA_TOKEN       Auth token (default: local-dev-token)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import click
import httpx
from rich.console import Console
from rich.json import JSON as RichJSON
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
from rich.spinner import Spinner

# Default configuration
DEFAULT_URL = os.environ.get("OBSCURA_URL", "http://localhost:8080")
DEFAULT_TOKEN = os.environ.get("OBSCURA_TOKEN", "local-dev-token")

console = Console()


class ObscuraCLI:
    """CLI client for Obscura API."""
    
    def __init__(self, base_url: str = DEFAULT_URL, token: str = DEFAULT_TOKEN):
        self.base_url = base_url
        self.token = token
        self.client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=300.0,
        )
    
    def spawn_agent(
        self,
        name: str,
        model: str = "claude",
        system_prompt: str = "",
        memory_namespace: str = "cli",
    ) -> dict[str, Any]:
        """Spawn a new agent."""
        resp = self.client.post(
            "/api/v1/agents",
            json={
                "name": name,
                "model": model,
                "system_prompt": system_prompt,
                "memory_namespace": memory_namespace,
            }
        )
        resp.raise_for_status()
        return resp.json()
    
    def run_agent(self, agent_id: str, prompt: str, **context) -> dict[str, Any]:
        """Run a task on an agent."""
        resp = self.client.post(
            f"/api/v1/agents/{agent_id}/run",
            json={"prompt": prompt, "context": context},
        )
        resp.raise_for_status()
        return resp.json()
    
    def get_agent(self, agent_id: str) -> dict[str, Any]:
        """Get agent status."""
        resp = self.client.get(f"/api/v1/agents/{agent_id}")
        resp.raise_for_status()
        return resp.json()
    
    def list_agents(self, status: str | None = None) -> list[dict[str, Any]]:
        """List all agents."""
        params = {}
        if status:
            params["status"] = status
        resp = self.client.get("/api/v1/agents", params=params)
        resp.raise_for_status()
        return resp.json().get("agents", [])
    
    def stop_agent(self, agent_id: str) -> dict[str, Any]:
        """Stop an agent."""
        resp = self.client.delete(f"/api/v1/agents/{agent_id}")
        resp.raise_for_status()
        return resp.json()
    
    def stream_agent(self, agent_id: str, prompt: str, **context):
        """Stream agent output."""
        with self.client.stream(
            "POST",
            f"/api/v1/agents/{agent_id}/run",
            json={"prompt": prompt, "context": context},
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if line:
                    yield line
    
    def set_memory(self, key: str, value: Any, namespace: str = "cli") -> None:
        """Store a value."""
        resp = self.client.post(
            f"/api/v1/memory/{namespace}/{key}",
            json={"value": value},
        )
        resp.raise_for_status()
    
    def get_memory(self, key: str, namespace: str = "cli") -> Any | None:
        """Get a value."""
        resp = self.client.get(f"/api/v1/memory/{namespace}/{key}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("value")
    
    def delete_memory(self, key: str, namespace: str = "cli") -> bool:
        """Delete a value."""
        resp = self.client.delete(f"/api/v1/memory/{namespace}/{key}")
        return resp.status_code == 200
    
    def list_memory(self, namespace: str | None = None) -> list[dict]:
        """List memory keys."""
        params = {}
        if namespace:
            params["namespace"] = namespace
        resp = self.client.get("/api/v1/memory", params=params)
        resp.raise_for_status()
        return resp.json().get("keys", [])
    
    def search_memory(self, query: str) -> list[dict]:
        """Search memory."""
        resp = self.client.get("/api/v1/memory/search", params={"q": query})
        resp.raise_for_status()
        return resp.json().get("results", [])
    
    def remember(self, text: str, key: str | None = None, namespace: str = "semantic") -> str:
        """Store text with semantic embedding."""
        if key is None:
            import time
            key = f"mem_{int(time.time())}"
        resp = self.client.post(
            f"/api/v1/vector-memory/{namespace}/{key}",
            json={"text": text, "metadata": {"source": "cli"}},
        )
        resp.raise_for_status()
        return key
    
    def recall(self, query: str, top_k: int = 3) -> list[dict]:
        """Semantic search."""
        resp = self.client.get(
            "/api/v1/vector-memory/search",
            params={"q": query, "top_k": top_k},
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
    
    def health(self) -> dict[str, Any]:
        """Check server health."""
        resp = self.client.get("/health")
        resp.raise_for_status()
        return resp.json()


# Create CLI group
@click.group()
@click.option("--url", default=DEFAULT_URL, help="Obscura API URL")
@click.option("--token", default=DEFAULT_TOKEN, help="Auth token")
@click.pass_context
def cli(ctx: click.Context, url: str, token: str):
    """Obscura CLI — Manage agents and memory."""
    ctx.ensure_object(dict)
    ctx.obj["client"] = ObscuraCLI(url, token)


# Agent commands
@cli.group()
def agent():
    """Agent management commands."""
    pass


@agent.command("spawn")
@click.option("--name", "-n", required=True, help="Agent name")
@click.option("--model", "-m", default="copilot", help="Model (copilot or claude)")
@click.option("--system-prompt", "-s", default="", help="System instructions")
@click.option("--namespace", default="cli", help="Memory namespace")
@click.pass_context
def agent_spawn(ctx, name, model, system_prompt, namespace):
    """Spawn a new agent."""
    client: ObscuraCLI = ctx.obj["client"]
    
    with console.status(f"[bold green]Spawning agent '{name}'..."):
        result = client.spawn_agent(name, model, system_prompt, namespace)
    
    console.print(Panel(
        f"[bold green]Agent spawned successfully![/]\n\n"
        f"[cyan]ID:[/] {result['agent_id']}\n"
        f"[cyan]Name:[/] {result['name']}\n"
        f"[cyan]Status:[/] {result['status']}\n"
        f"[cyan]Created:[/] {result['created_at']}",
        title="Agent Created",
        border_style="green"
    ))
    
    # Copy to clipboard hint
    console.print(f"\n[dim]Run: [bold]obscura agent run {result['agent_id']} --prompt 'your task'[/][/dim]")


@agent.command("run")
@click.argument("agent_id")
@click.option("--prompt", "-p", required=True, help="Task prompt")
@click.option("--stream", is_flag=True, help="Stream output")
@click.pass_context
def agent_run(ctx, agent_id, prompt, stream):
    """Run a task on an agent."""
    client: ObscuraCLI = ctx.obj["client"]
    
    if stream:
        console.print(f"[bold cyan]Running agent {agent_id}...[/]\n")
        # TODO: Implement streaming
        console.print("[yellow]Streaming not yet implemented in CLI[/]")
    else:
        with console.status("[bold green]Running task..."):
            result = client.run_agent(agent_id, prompt)
        
        console.print(Panel(
            result.get("result", "No result"),
            title=f"Agent Result ({result.get('status', 'unknown')})",
            border_style="blue"
        ))


@agent.command("list")
@click.option("--status", help="Filter by status")
@click.pass_context
def agent_list(ctx, status):
    """List all agents."""
    client: ObscuraCLI = ctx.obj["client"]
    
    agents = client.list_agents(status)
    
    if not agents:
        console.print("[yellow]No agents found.[/]")
        return
    
    table = Table(title="Agents")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Status", style="yellow")
    table.add_column("Model", style="magenta")
    table.add_column("Created", style="dim")
    
    for a in agents:
        table.add_row(
            a["agent_id"][:12],
            a["name"],
            a["status"],
            a["model"],
            a["created_at"][:19],
        )
    
    console.print(table)


@agent.command("status")
@click.argument("agent_id")
@click.pass_context
def agent_status(ctx, agent_id):
    """Get agent status."""
    client: ObscuraCLI = ctx.obj["client"]
    
    result = client.get_agent(agent_id)
    
    console.print(Panel(
        f"[cyan]ID:[/] {result['agent_id']}\n"
        f"[cyan]Name:[/] {result['name']}\n"
        f"[cyan]Status:[/] {result['status']}\n"
        f"[cyan]Iterations:[/] {result['iteration_count']}\n"
        f"[cyan]Created:[/] {result['created_at']}\n"
        f"[cyan]Updated:[/] {result['updated_at']}",
        title="Agent Status",
        border_style="blue"
    ))


@agent.command("stop")
@click.argument("agent_id")
@click.pass_context
def agent_stop(ctx, agent_id):
    """Stop an agent."""
    client: ObscuraCLI = ctx.obj["client"]
    
    with console.status(f"[bold yellow]Stopping agent {agent_id}..."):
        result = client.stop_agent(agent_id)
    
    console.print(f"[bold green]Agent {agent_id} stopped.[/]")


@agent.command("quick")
@click.option("--name", "-n", default="quick-agent", help="Agent name")
@click.option("--model", "-m", default="copilot", help="Model")
@click.option("--prompt", "-p", required=True, help="Task prompt")
@click.pass_context
def agent_quick(ctx, name, model, prompt):
    """Quick one-off agent: spawn, run, stop."""
    client: ObscuraCLI = ctx.obj["client"]
    
    with console.status("[bold green]Spawning agent..."):
        agent = client.spawn_agent(name, model)
        agent_id = agent["agent_id"]
    
    try:
        with console.status("[bold blue]Running task..."):
            result = client.run_agent(agent_id, prompt)
        
        console.print(Panel(
            result.get("result", "No result"),
            title=f"Result from {name}",
            border_style="green"
        ))
    finally:
        client.stop_agent(agent_id)


# Memory commands
@cli.group()
def memory():
    """Memory management commands."""
    pass


@memory.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--namespace", "-n", default="cli", help="Namespace")
@click.option("--json", "is_json", is_flag=True, help="Parse value as JSON")
@click.pass_context
def memory_set(ctx, key, value, namespace, is_json):
    """Store a value in memory."""
    client: ObscuraCLI = ctx.obj["client"]
    
    if is_json:
        value = json.loads(value)
    
    client.set_memory(key, value, namespace)
    console.print(f"[bold green]Set {namespace}:{key}[/]")


@memory.command("get")
@click.argument("key")
@click.option("--namespace", "-n", default="cli", help="Namespace")
@click.pass_context
def memory_get(ctx, key, namespace):
    """Get a value from memory."""
    client: ObscuraCLI = ctx.obj["client"]
    
    value = client.get_memory(key, namespace)
    
    if value is None:
        console.print(f"[yellow]Key {namespace}:{key} not found.[/]")
    else:
        console.print(RichJSON(json.dumps(value, indent=2)))


@memory.command("delete")
@click.argument("key")
@click.option("--namespace", "-n", default="cli", help="Namespace")
@click.pass_context
def memory_delete(ctx, key, namespace):
    """Delete a value from memory."""
    client: ObscuraCLI = ctx.obj["client"]
    
    if client.delete_memory(key, namespace):
        console.print(f"[bold green]Deleted {namespace}:{key}[/]")
    else:
        console.print(f"[yellow]Key {namespace}:{key} not found.[/]")


@memory.command("list")
@click.option("--namespace", "-n", help="Filter by namespace")
@click.pass_context
def memory_list(ctx, namespace):
    """List all memory keys."""
    client: ObscuraCLI = ctx.obj["client"]
    
    keys = client.list_memory(namespace)
    
    if not keys:
        console.print("[yellow]No keys found.[/]")
        return
    
    table = Table(title="Memory Keys")
    table.add_column("Namespace", style="cyan")
    table.add_column("Key", style="green")
    
    for k in keys:
        table.add_row(k["namespace"], k["key"])
    
    console.print(table)


@memory.command("search")
@click.argument("query")
@click.pass_context
def memory_search(ctx, query):
    """Search memory."""
    client: ObscuraCLI = ctx.obj["client"]
    
    results = client.search_memory(query)
    
    if not results:
        console.print("[yellow]No results found.[/]")
        return
    
    for r in results:
        console.print(Panel(
            str(r.get("value", "")),
            title=f"{r['namespace']}:{r['key']}",
            border_style="blue"
        ))


# Vector memory commands
@cli.group(name="vector")
def vector_cmd():
    """Vector/semantic memory commands."""
    pass


@vector_cmd.command("remember")
@click.argument("text")
@click.option("--key", "-k", help="Optional key")
@click.option("--namespace", "-n", default="semantic", help="Namespace")
@click.pass_context
def vector_remember(ctx, text, key, namespace):
    """Store text with semantic embedding."""
    client: ObscuraCLI = ctx.obj["client"]
    
    result_key = client.remember(text, key, namespace)
    console.print(f"[bold green]Remembered as {namespace}:{result_key}[/]")


@vector_cmd.command("recall")
@click.argument("query")
@click.option("--top-k", "-k", default=3, help="Number of results")
@click.pass_context
def vector_recall(ctx, query, top_k):
    """Recall semantically similar memories."""
    client: ObscuraCLI = ctx.obj["client"]
    
    results = client.recall(query, top_k)
    
    if not results:
        console.print("[yellow]No memories found.[/]")
        return
    
    for i, r in enumerate(results, 1):
        score = r.get("score", 0)
        console.print(Panel(
            r.get("text", ""),
            title=f"#{i} ({score:.2f}) {r['namespace']}:{r['key']}",
            border_style="green" if score > 0.8 else "yellow" if score > 0.5 else "red"
        ))


# Server command
@cli.command("serve")
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", "-p", default=8080, help="Bind port")
@click.option("--reload", is_flag=True, help="Auto-reload on code changes")
@click.option("--workers", "-w", default=1, help="Number of workers")
def serve(host, port, reload, workers):
    """Start the Obscura server."""
    try:
        import uvicorn
    except ImportError:
        console.print("[bold red]Error:[/] uvicorn not installed. Run: pip install uvicorn")
        sys.exit(1)
    
    console.print(f"[bold green]Starting Obscura server on {host}:{port}...[/]")
    
    uvicorn.run(
        "sdk.server:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        workers=workers if not reload else 1,
    )


# TUI command
@cli.command("tui")
@click.option("--backend", "-b", default="copilot", type=click.Choice(["copilot", "claude"]), help="Backend to use")
@click.option("--model", default=None, help="Model ID override")
@click.option("--cwd", default=".", help="Working directory")
@click.option("--session", "-s", default=None, help="Resume a saved session by ID")
@click.option("--mode", default="ask", type=click.Choice(["ask", "plan", "code", "diff"]), help="Initial mode")
def tui(backend, model, cwd, session, mode):
    """Launch interactive TUI."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    try:
        from sdk.tui.app import run_tui
        run_tui(
            backend=backend,
            model=model,
            cwd=cwd,
            session=session,
            mode=mode,
        )
    except ImportError as e:
        console.print(f"[bold red]Error:[/] TUI dependencies not installed: {e}")
        console.print(f"[yellow]Run: pip install 'obscura[tui]'[/]")
        sys.exit(1)


# Health check
@cli.command("health")
@click.pass_context
def health_check(ctx):
    """Check server health."""
    client: ObscuraCLI = ctx.obj["client"]
    
    try:
        result = client.health()
        console.print(f"[bold green]✓ Server is healthy:[/] {result}")
    except Exception as e:
        console.print(f"[bold red]✗ Server error:[/] {e}")
        sys.exit(1)


# Main entry point
def main():
    """Entry point for the CLI."""
    try:
        cli()
    except httpx.HTTPError as e:
        console.print(f"[bold red]API Error:[/] {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/]")
        sys.exit(130)


if __name__ == "__main__":
    main()
