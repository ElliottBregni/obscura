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

import argparse
import json
import os
import sys
import time as _time_mod
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import click
import httpx
from rich.console import Console
from rich.json import JSON as RichJSON
from rich.table import Table
from rich.panel import Panel

# Default configuration
DEFAULT_URL: str = os.environ.get("OBSCURA_URL", "http://localhost:8080")
DEFAULT_TOKEN: str = os.environ.get("OBSCURA_TOKEN", "local-dev-token")

console: Console = Console()


def _get_client(ctx: click.Context) -> ObscuraCLI:
    """Extract the ObscuraCLI client from click context."""
    obj: dict[str, Any] = cast(dict[str, Any], ctx.ensure_object(dict))
    client: ObscuraCLI = obj["client"]
    return client


class ObscuraCLI:
    """CLI client for Obscura API."""

    def __init__(self, base_url: str = DEFAULT_URL, token: str = DEFAULT_TOKEN) -> None:
        self.base_url: str = base_url
        self.token: str = token
        self.client: httpx.Client = httpx.Client(
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
        resp: httpx.Response = self.client.post(
            "/api/v1/agents",
            json={
                "name": name,
                "model": model,
                "system_prompt": system_prompt,
                "memory_namespace": memory_namespace,
            },
        )
        resp.raise_for_status()
        result: Any = resp.json()
        return result

    def run_agent(self, agent_id: str, prompt: str, **context: Any) -> dict[str, Any]:
        """Run a task on an agent."""
        resp: httpx.Response = self.client.post(
            f"/api/v1/agents/{agent_id}/run",
            json={"prompt": prompt, "context": context},
        )
        resp.raise_for_status()
        result: Any = resp.json()
        return result

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        """Get agent status."""
        resp: httpx.Response = self.client.get(f"/api/v1/agents/{agent_id}")
        resp.raise_for_status()
        result: Any = resp.json()
        return result

    def list_agents(self, status: str | None = None) -> list[dict[str, Any]]:
        """List all agents."""
        params: dict[str, str] = {}
        if status:
            params["status"] = status
        resp: httpx.Response = self.client.get("/api/v1/agents", params=params)
        resp.raise_for_status()
        result: Any = resp.json()
        agents: list[dict[str, Any]] = result.get("agents", [])
        return agents

    def stop_agent(self, agent_id: str) -> dict[str, Any]:
        """Stop an agent."""
        resp: httpx.Response = self.client.delete(f"/api/v1/agents/{agent_id}")
        resp.raise_for_status()
        result: Any = resp.json()
        return result

    def stream_agent(
        self, agent_id: str, prompt: str, **context: Any
    ) -> Generator[str, None, None]:
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
        resp: httpx.Response = self.client.post(
            f"/api/v1/memory/{namespace}/{key}",
            json={"value": value},
        )
        resp.raise_for_status()

    def get_memory(self, key: str, namespace: str = "cli") -> Any | None:
        """Get a value."""
        resp: httpx.Response = self.client.get(f"/api/v1/memory/{namespace}/{key}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        result: Any = resp.json()
        return result.get("value")

    def delete_memory(self, key: str, namespace: str = "cli") -> bool:
        """Delete a value."""
        resp: httpx.Response = self.client.delete(f"/api/v1/memory/{namespace}/{key}")
        return resp.status_code == 200

    def list_memory(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """List memory keys."""
        params: dict[str, str] = {}
        if namespace:
            params["namespace"] = namespace
        resp: httpx.Response = self.client.get("/api/v1/memory", params=params)
        resp.raise_for_status()
        result: Any = resp.json()
        keys: list[dict[str, Any]] = result.get("keys", [])
        return keys

    def search_memory(self, query: str) -> list[dict[str, Any]]:
        """Search memory."""
        resp: httpx.Response = self.client.get(
            "/api/v1/memory/search", params={"q": query}
        )
        resp.raise_for_status()
        result: Any = resp.json()
        results: list[dict[str, Any]] = result.get("results", [])
        return results

    def remember(
        self, text: str, key: str | None = None, namespace: str = "semantic"
    ) -> str:
        """Store text with semantic embedding."""
        if key is None:
            import time

            key = f"mem_{int(time.time())}"
        resp: httpx.Response = self.client.post(
            f"/api/v1/vector-memory/{namespace}/{key}",
            json={"text": text, "metadata": {"source": "cli"}},
        )
        resp.raise_for_status()
        return key

    def recall(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """Semantic search."""
        resp: httpx.Response = self.client.get(
            "/api/v1/vector-memory/search",
            params={"q": query, "top_k": top_k},
        )
        resp.raise_for_status()
        result: Any = resp.json()
        results: list[dict[str, Any]] = result.get("results", [])
        return results

    def health(self) -> dict[str, Any]:
        """Check server health."""
        resp: httpx.Response = self.client.get("/health")
        resp.raise_for_status()
        result: Any = resp.json()
        return result


# Create CLI group
@click.group()
@click.option("--url", default=DEFAULT_URL, help="Obscura API URL")
@click.option("--token", default=DEFAULT_TOKEN, help="Auth token")
@click.pass_context
def cli(ctx: click.Context, url: str, token: str) -> None:
    """Obscura CLI — Manage agents and memory."""
    obj: dict[str, Any] = cast(dict[str, Any], ctx.ensure_object(dict))
    obj["client"] = ObscuraCLI(url, token)


# Agent commands
@cli.group()
def agent() -> None:
    """Agent management commands."""
    pass


@agent.command("spawn")
@click.option("--name", "-n", required=True, help="Agent name")
@click.option("--model", "-m", default="copilot", help="Model (copilot or claude)")
@click.option("--system-prompt", "-s", default="", help="System instructions")
@click.option("--namespace", default="cli", help="Memory namespace")
@click.pass_context
def agent_spawn(
    ctx: click.Context, name: str, model: str, system_prompt: str, namespace: str
) -> None:
    """Spawn a new agent."""
    client: ObscuraCLI = _get_client(ctx)

    with console.status(f"[bold green]Spawning agent '{name}'..."):
        result: dict[str, Any] = client.spawn_agent(
            name, model, system_prompt, namespace
        )

    console.print(
        Panel(
            f"[bold green]Agent spawned successfully![/]\n\n"
            f"[cyan]ID:[/] {result['agent_id']}\n"
            f"[cyan]Name:[/] {result['name']}\n"
            f"[cyan]Status:[/] {result['status']}\n"
            f"[cyan]Created:[/] {result['created_at']}",
            title="Agent Created",
            border_style="green",
        )
    )

    # Copy to clipboard hint
    console.print(
        f"\n[dim]Run: [bold]obscura agent run {result['agent_id']} --prompt 'your task'[/][/dim]"
    )


@agent.command("run")
@click.argument("agent_id")
@click.option("--prompt", "-p", required=True, help="Task prompt")
@click.option("--stream", is_flag=True, help="Stream output")
@click.pass_context
def agent_run(ctx: click.Context, agent_id: str, prompt: str, stream: bool) -> None:
    """Run a task on an agent."""
    client: ObscuraCLI = _get_client(ctx)

    if stream:
        console.print(f"[bold cyan]Running agent {agent_id}...[/]\n")
        # TODO: Implement streaming
        console.print("[yellow]Streaming not yet implemented in CLI[/]")
    else:
        with console.status("[bold green]Running task..."):
            result: dict[str, Any] = client.run_agent(agent_id, prompt)

        console.print(
            Panel(
                str(result.get("result", "No result")),
                title=f"Agent Result ({result.get('status', 'unknown')})",
                border_style="blue",
            )
        )


@agent.command("list")
@click.option("--status", help="Filter by status")
@click.pass_context
def agent_list(ctx: click.Context, status: str | None) -> None:
    """List all agents."""
    client: ObscuraCLI = _get_client(ctx)

    agents: list[dict[str, Any]] = client.list_agents(status)

    if not agents:
        console.print("[yellow]No agents found.[/]")
        return

    table: Table = Table(title="Agents")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Status", style="yellow")
    table.add_column("Model", style="magenta")
    table.add_column("Created", style="dim")

    for a in agents:
        table.add_row(
            str(a["agent_id"])[:12],
            str(a["name"]),
            str(a["status"]),
            str(a["model"]),
            str(a["created_at"])[:19],
        )

    console.print(table)


@agent.command("status")
@click.argument("agent_id")
@click.pass_context
def agent_status(ctx: click.Context, agent_id: str) -> None:
    """Get agent status."""
    client: ObscuraCLI = _get_client(ctx)

    result: dict[str, Any] = client.get_agent(agent_id)

    console.print(
        Panel(
            f"[cyan]ID:[/] {result['agent_id']}\n"
            f"[cyan]Name:[/] {result['name']}\n"
            f"[cyan]Status:[/] {result['status']}\n"
            f"[cyan]Iterations:[/] {result['iteration_count']}\n"
            f"[cyan]Created:[/] {result['created_at']}\n"
            f"[cyan]Updated:[/] {result['updated_at']}",
            title="Agent Status",
            border_style="blue",
        )
    )


@agent.command("stop")
@click.argument("agent_id")
@click.pass_context
def agent_stop(ctx: click.Context, agent_id: str) -> None:
    """Stop an agent."""
    client: ObscuraCLI = _get_client(ctx)

    with console.status(f"[bold yellow]Stopping agent {agent_id}..."):
        client.stop_agent(agent_id)

    console.print(f"[bold green]Agent {agent_id} stopped.[/]")


@agent.command("quick")
@click.option("--name", "-n", default="quick-agent", help="Agent name")
@click.option("--model", "-m", default="copilot", help="Model")
@click.option("--prompt", "-p", required=True, help="Task prompt")
@click.pass_context
def agent_quick(ctx: click.Context, name: str, model: str, prompt: str) -> None:
    """Quick one-off agent: spawn, run, stop."""
    client: ObscuraCLI = _get_client(ctx)

    with console.status("[bold green]Spawning agent..."):
        spawned: dict[str, Any] = client.spawn_agent(name, model)
        agent_id: str = str(spawned["agent_id"])

    try:
        with console.status("[bold blue]Running task..."):
            result: dict[str, Any] = client.run_agent(agent_id, prompt)

        console.print(
            Panel(
                str(result.get("result", "No result")),
                title=f"Result from {name}",
                border_style="green",
            )
        )
    finally:
        client.stop_agent(agent_id)


# -- Long-running agent commands -------------------------------------------


@agent.command("loop")
@click.option("--name", "-n", default="loop-agent", help="Agent name")
@click.option("--model", "-m", default="copilot", help="Model backend")
@click.option("--system-prompt", "-s", default="", help="System instructions")
@click.option("--max-turns", default=25, help="Max turns per input")
def agent_loop(name: str, model: str, system_prompt: str, max_turns: int) -> None:
    """Start a long-running loop agent (interactive prompt loop)."""
    import asyncio

    async def _run() -> None:
        from obscura.agent.interaction import InteractionBus
        from obscura.agent.loop_agent import LoopAgent
        from obscura.core.client import ObscuraClient
        from obscura.notifications.native import NativeNotifier

        bus = InteractionBus()

        # Wire native notifications
        notifier = NativeNotifier()

        async def _on_attention(req: Any) -> None:
            await notifier.attention(
                title=req.agent_name,
                message=req.message,
                priority=req.priority,
                actions=list(req.actions),
            )

        bus.on_attention(_on_attention)

        async with ObscuraClient(
            model, system_prompt=system_prompt
        ) as client:
            agent = LoopAgent(
                client,
                name=name,
                interaction_bus=bus,
                max_turns_per_input=max_turns,
            )

            # Start the loop in the background
            loop_task = asyncio.create_task(agent.run_forever())

            console.print(
                f"[bold green]Loop agent '{name}' started[/] (model={model})"
            )
            console.print("[dim]Type messages below. 'exit' or Ctrl-C to stop.[/]\n")

            try:
                while not agent.stopped:
                    try:
                        user_input: str = await asyncio.get_event_loop().run_in_executor(
                            None, lambda: click.prompt("You", type=str)
                        )
                    except (EOFError, click.Abort):
                        break
                    if user_input.strip().lower() in ("exit", "quit"):
                        break
                    await agent.send(user_input)
            except KeyboardInterrupt:
                pass
            finally:
                await agent.stop()
                loop_task.cancel()
                try:
                    await loop_task
                except asyncio.CancelledError:
                    pass

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/]")


@agent.command("daemon")
@click.option("--name", "-n", default="daemon", help="Agent name")
@click.option("--model", "-m", default="copilot", help="Model backend")
@click.option("--system-prompt", "-s", default="", help="System instructions")
@click.option(
    "--trigger",
    "-t",
    multiple=True,
    help=(
        'Trigger spec: "schedule:CRON:PROMPT" e.g. '
        '"schedule:*/5 * * * *:check system health"'
    ),
)
def agent_daemon(
    name: str, model: str, system_prompt: str, trigger: tuple[str, ...]
) -> None:
    """Start a daemon agent that reacts to triggers."""
    import asyncio

    async def _run() -> None:
        from obscura.agent.daemon_agent import DaemonAgent, ScheduleTrigger, Trigger
        from obscura.agent.interaction import AttentionPriority, InteractionBus
        from obscura.core.client import ObscuraClient
        from obscura.notifications.native import NativeNotifier

        bus = InteractionBus()
        notifier = NativeNotifier()

        async def _on_attention(req: Any) -> None:
            await notifier.attention(
                title=req.agent_name,
                message=req.message,
                priority=req.priority,
                actions=list(req.actions),
            )

        bus.on_attention(_on_attention)

        # Parse trigger specs
        triggers: list[Trigger] = []
        for spec in trigger:
            parts = spec.split(":", 2)
            if len(parts) >= 3 and parts[0] == "schedule":
                triggers.append(
                    ScheduleTrigger(
                        cron=parts[1],
                        prompt=parts[2],
                        description=parts[2][:50],
                        notify_user=True,
                        priority=AttentionPriority.NORMAL,
                    )
                )
            else:
                console.print(f"[yellow]Unknown trigger spec: {spec}[/]")

        async with ObscuraClient(
            model, system_prompt=system_prompt
        ) as client:
            daemon = DaemonAgent(
                client,
                name=name,
                triggers=triggers,
                interaction_bus=bus,
            )

            console.print(
                f"[bold green]Daemon agent '{name}' started[/] "
                f"(model={model}, triggers={len(triggers)})"
            )
            console.print("[dim]Press Ctrl-C to stop.[/]\n")

            try:
                await daemon.run_forever()
            except KeyboardInterrupt:
                await daemon.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/]")


@agent.command("send")
@click.argument("agent_id")
@click.option("--message", "-m", required=True, help="Message to send")
@click.pass_context
def agent_send(ctx: click.Context, agent_id: str, message: str) -> None:
    """Send a message to a running agent via WebSocket."""
    import asyncio

    async def _send() -> None:
        import websockets

        client: ObscuraCLI = _get_client(ctx)
        ws_url = client.base_url.replace("http://", "ws://").replace(
            "https://", "wss://"
        )
        url = f"{ws_url}/ws/agents/{agent_id}"

        async with websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {client.token}"},
        ) as ws:
            await ws.send(json.dumps({"type": "run", "prompt": message}))
            console.print(f"[bold green]Sent to {agent_id}[/]: {message}")

            # Read response chunks
            while True:
                raw = await ws.recv()
                data: dict[str, Any] = json.loads(str(raw))
                if data.get("type") == "chunk":
                    console.print(str(data.get("text", "")), end="")
                elif data.get("type") == "done":
                    console.print()
                    break
                elif data.get("type") == "error":
                    console.print(f"\n[red]Error: {data.get('message')}[/]")
                    break
                elif data.get("type") == "attention_request":
                    console.print(
                        f"\n[bold yellow]Attention:[/] {data.get('message')}"
                    )
                    console.print(
                        f"  Actions: {', '.join(data.get('actions', []))}"
                    )

    try:
        asyncio.run(_send())
    except ImportError:
        console.print(
            "[red]websockets not installed. Run: pip install websockets[/]"
        )
    except Exception as e:
        console.print(f"[red]Error: {e}[/]")


@agent.command("supervisor")
@click.option(
    "--config",
    "-c",
    default="~/.obscura/agents.yaml",
    help="Path to agents YAML config",
)
def agent_supervisor(config: str) -> None:
    """Start the agent supervisor (keeps configured agents alive)."""
    import asyncio
    from pathlib import Path

    async def _run() -> None:
        from obscura.agent.supervisor import AgentSupervisor

        cli_user = _resolve_cli_user()
        sup = AgentSupervisor(
            config_path=Path(config),
            user=cli_user,
        )
        console.print(f"[bold green]Supervisor starting[/] (config={config})")
        console.print("[dim]Press Ctrl-C to stop.[/]\n")
        await sup.run_forever()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Supervisor stopped.[/]")


# Memory commands
@cli.group()
def memory() -> None:
    """Memory management commands."""
    pass


@memory.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--namespace", "-n", default="cli", help="Namespace")
@click.option("--json", "is_json", is_flag=True, help="Parse value as JSON")
@click.pass_context
def memory_set(
    ctx: click.Context, key: str, value: str, namespace: str, is_json: bool
) -> None:
    """Store a value in memory."""
    client: ObscuraCLI = _get_client(ctx)

    parsed_value: Any = value
    if is_json:
        parsed_value = json.loads(value)

    client.set_memory(key, parsed_value, namespace)
    console.print(f"[bold green]Set {namespace}:{key}[/]")


@memory.command("get")
@click.argument("key")
@click.option("--namespace", "-n", default="cli", help="Namespace")
@click.pass_context
def memory_get(ctx: click.Context, key: str, namespace: str) -> None:
    """Get a value from memory."""
    client: ObscuraCLI = _get_client(ctx)

    value: Any | None = client.get_memory(key, namespace)

    if value is None:
        console.print(f"[yellow]Key {namespace}:{key} not found.[/]")
    else:
        console.print(RichJSON(json.dumps(value, indent=2)))


@memory.command("delete")
@click.argument("key")
@click.option("--namespace", "-n", default="cli", help="Namespace")
@click.pass_context
def memory_delete(ctx: click.Context, key: str, namespace: str) -> None:
    """Delete a value from memory."""
    client: ObscuraCLI = _get_client(ctx)

    if client.delete_memory(key, namespace):
        console.print(f"[bold green]Deleted {namespace}:{key}[/]")
    else:
        console.print(f"[yellow]Key {namespace}:{key} not found.[/]")


@memory.command("list")
@click.option("--namespace", "-n", help="Filter by namespace")
@click.pass_context
def memory_list(ctx: click.Context, namespace: str | None) -> None:
    """List all memory keys."""
    client: ObscuraCLI = _get_client(ctx)

    keys: list[dict[str, Any]] = client.list_memory(namespace)

    if not keys:
        console.print("[yellow]No keys found.[/]")
        return

    table: Table = Table(title="Memory Keys")
    table.add_column("Namespace", style="cyan")
    table.add_column("Key", style="green")

    for k in keys:
        table.add_row(str(k["namespace"]), str(k["key"]))

    console.print(table)


@memory.command("search")
@click.argument("query")
@click.pass_context
def memory_search(ctx: click.Context, query: str) -> None:
    """Search memory."""
    client: ObscuraCLI = _get_client(ctx)

    results: list[dict[str, Any]] = client.search_memory(query)

    if not results:
        console.print("[yellow]No results found.[/]")
        return

    for r in results:
        console.print(
            Panel(
                str(r.get("value", "")),
                title=f"{r['namespace']}:{r['key']}",
                border_style="blue",
            )
        )


# Vector memory commands
@cli.group(name="vector")
def vector_cmd() -> None:
    """Vector/semantic memory commands."""
    pass


@vector_cmd.command("remember")
@click.argument("text")
@click.option("--key", "-k", help="Optional key")
@click.option("--namespace", "-n", default="semantic", help="Namespace")
@click.pass_context
def vector_remember(
    ctx: click.Context, text: str, key: str | None, namespace: str
) -> None:
    """Store text with semantic embedding."""
    client: ObscuraCLI = _get_client(ctx)

    result_key: str = client.remember(text, key, namespace)
    console.print(f"[bold green]Remembered as {namespace}:{result_key}[/]")


@vector_cmd.command("recall")
@click.argument("query")
@click.option("--top-k", "-k", default=3, help="Number of results")
@click.pass_context
def vector_recall(ctx: click.Context, query: str, top_k: int) -> None:
    """Recall semantically similar memories."""
    client: ObscuraCLI = _get_client(ctx)

    results: list[dict[str, Any]] = client.recall(query, top_k)

    if not results:
        console.print("[yellow]No memories found.[/]")
        return

    for i, r in enumerate(results, 1):
        score: Any = r.get("score", 0)
        console.print(
            Panel(
                str(r.get("text", "")),
                title=f"#{i} ({score:.2f}) {r['namespace']}:{r['key']}",
                border_style="green"
                if score > 0.8
                else "yellow"
                if score > 0.5
                else "red",
            )
        )


# Server command
@cli.command("serve")
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", "-p", default=8080, help="Bind port")
@click.option("--reload", is_flag=True, help="Auto-reload on code changes")
@click.option("--workers", "-w", default=1, help="Number of workers")
def serve(host: str, port: int, reload: bool, workers: int) -> None:
    """Start the Obscura server."""
    try:
        import uvicorn
    except ImportError:
        console.print(
            "[bold red]Error:[/] uvicorn not installed. Run: pip install uvicorn"
        )
        sys.exit(1)

    console.print(f"[bold green]Starting Obscura server on {host}:{port}...[/]")

    uvicorn.run(
        "obscura.server:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        workers=workers if not reload else 1,
    )


# TUI command
@cli.command("tui")
@click.option(
    "--backend",
    "-b",
    default="copilot",
    type=click.Choice(["copilot", "claude"]),
    help="Backend to use",
)
@click.option("--model", default=None, help="Model ID override")
@click.option("--cwd", default=".", help="Working directory")
@click.option("--session", "-s", default=None, help="Resume a saved session by ID")
@click.option(
    "--mode",
    default="ask",
    type=click.Choice(["ask", "plan", "code", "diff"]),
    help="Initial mode",
)
def tui(
    backend: str, model: str | None, cwd: str, session: str | None, mode: str
) -> None:
    """Launch interactive TUI."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    try:
        from obscura.tui.app import run_tui

        run_tui(
            backend=backend,
            model=model,
            cwd=cwd,
            session=session,
            mode=mode,
        )
    except ImportError as e:
        console.print(f"[bold red]Error:[/] TUI dependencies not installed: {e}")
        console.print("[yellow]Run: pip install 'obscura[tui]'[/]")
        sys.exit(1)


# Health check
@cli.command("health")
@click.pass_context
def health_check(ctx: click.Context) -> None:
    """Check server health."""
    client: ObscuraCLI = _get_client(ctx)

    try:
        result: dict[str, Any] = client.health()
        console.print(f"[bold green]Server is healthy:[/] {result}")
    except Exception as e:
        console.print(f"[bold red]Server error:[/] {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Chat / Passthrough Helpers
# ---------------------------------------------------------------------------


def _resolve_cli_user() -> Any:
    """Synthetic AuthenticatedUser for CLI context (no auth server)."""
    import getpass

    from obscura.auth.models import AuthenticatedUser

    return AuthenticatedUser(
        user_id=f"cli:{getpass.getuser()}",
        email="",
        roles=("admin",),
        org_id=None,
        token_type="cli",
        raw_token="",
    )


def _parse_tool_policy(raw: str) -> Any:
    """Parse ``auto|none|required:<name>`` into a ToolChoice."""
    from obscura.core.types import ToolChoice

    if raw == "none":
        return ToolChoice.none()
    if raw.startswith("required:"):
        return ToolChoice.required(raw.split(":", 1)[1])
    return ToolChoice.auto()


def _render_event(
    event: Any,
    *,
    show_aux: bool = True,
    show_thinking: bool = True,
) -> None:
    """Render an AgentEvent to the Rich console."""
    from obscura.core.types import AgentEventKind

    if event.kind == AgentEventKind.TEXT_DELTA:
        console.print(event.text, end="")
    elif event.kind == AgentEventKind.THINKING_DELTA and show_thinking:
        console.print(f"[dim italic]{event.text}[/]", end="")
    elif event.kind == AgentEventKind.TOOL_CALL and show_aux:
        console.print(f"[dim][tool] {event.tool_name}[/]")
    elif event.kind == AgentEventKind.TOOL_RESULT and show_aux:
        console.print(f"[dim][result] {event.tool_result[:80]}[/]")


def _load_memory_context(user: Any, prompt: str) -> str:
    """Best-effort memory context injection for a prompt."""
    from obscura.memory import MemoryStore

    parts: list[str] = []

    # 1) Text search in MemoryStore
    try:
        mem = MemoryStore.for_user(user)
        hits = mem.search(prompt)
        for key, value in hits[:3]:
            val_str = str(value)[:200] if not isinstance(value, str) else value[:200]
            parts.append(f"- {key}: {val_str}")
    except Exception:
        pass

    # 2) Semantic search via VectorMemoryStore (best-effort)
    try:
        from obscura.vector_memory import VectorMemoryStore

        vmem = VectorMemoryStore.for_user(user)
        similar = vmem.search_similar(prompt, top_k=3)
        for entry in similar:
            parts.append(f"- {entry.text[:200]}")
    except Exception:
        pass  # numpy/embedding not available

    return "\n".join(parts)


def _persist_transcript(
    user: Any,
    session_id: str,
    transcript: list[dict[str, str]],
    backend: str,
) -> None:
    """Dual persistence: MemoryStore + human-readable file."""
    import time
    from pathlib import Path

    # 1) MemoryStore (structured, searchable)
    try:
        from obscura.memory import MemoryStore

        mem = MemoryStore.for_user(user)
        mem.set(
            f"transcript:{session_id}",
            transcript,
            namespace="session",
        )
    except Exception:
        pass

    # 2) File system (human-readable)
    try:
        transcript_dir = Path.home() / ".obscura" / "transcripts"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        path = transcript_dir / f"chat_{backend}_{ts}.txt"
        lines: list[str] = []
        for msg in transcript:
            lines.append(f"[{msg.get('role', '?')}]\n{msg.get('content', '')}\n")
        path.write_text("\n".join(lines)[:50000])
        console.print(f"[dim]Transcript saved to {path}[/]")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Chat command (owned mode — direct backend, no server needed)
# ---------------------------------------------------------------------------


@cli.command("chat")
@click.argument("prompt", required=False)
@click.option(
    "--backend",
    "-b",
    default="openai",
    type=click.Choice(["openai", "moonshot", "claude", "copilot", "localllm"]),
    help="Backend to use",
)
@click.option("--model", "-m", default=None, help="Model ID override")
@click.option("--model-alias", default=None, help="Model alias (e.g. copilot_automation_safe)")
@click.option("--automation-safe", is_flag=True, help="Require automation-safe model (copilot only)")
@click.option("--system-prompt", "-s", default="", help="System instructions")
@click.option("--session", default=None, help="Session ID to resume")
@click.option("--list-sessions", is_flag=True, help="List available sessions and exit")
@click.option(
    "--no-stream", is_flag=True, help="Disable streaming (wait for full response)"
)
@click.option("--json-output", "json_out", is_flag=True, help="Output as JSON")
@click.option("--interactive", "-i", is_flag=True, help="Interactive multi-turn mode")
@click.option("--max-turns", default=10, help="Max agent loop turns")
@click.option(
    "--mode",
    default="unified",
    type=click.Choice(["unified", "native"]),
    help="Execution mode (unified = agent loop, native = raw SDK)",
)
@click.option(
    "--tools",
    default="on",
    type=click.Choice(["on", "off"]),
    help="Enable/disable tool calling",
)
@click.option(
    "--tool-policy",
    default="auto",
    help="Tool policy: auto|none|required:<name>",
)
@click.option(
    "--memory/--no-memory",
    "memory_enabled",
    default=True,
    help="Enable/disable memory injection and persistence",
)
@click.option(
    "--permission-mode",
    default="default",
    type=click.Choice(["default", "acceptEdits", "plan", "bypassPermissions"]),
    help="Claude permission mode (claude only)",
)
@click.option("--cwd", default=None, help="Working directory (claude only)")
def chat(
    prompt: str | None,
    backend: str,
    model: str | None,
    model_alias: str | None,
    automation_safe: bool,
    system_prompt: str,
    session: str | None,
    list_sessions: bool,
    no_stream: bool,
    json_out: bool,
    interactive: bool,
    max_turns: int,
    mode: str,
    tools: str,
    tool_policy: str,
    memory_enabled: bool,
    permission_mode: str,
    cwd: str | None,
) -> None:
    """Chat directly with a backend (no server required).

    \b
    Examples:
        obscura chat "explain this code" --backend openai
        obscura chat "explain this code" --backend moonshot --model kimi-2.5
        obscura chat --backend claude --interactive
        obscura chat "hello" --backend localllm --no-stream
        obscura chat "test" --mode native --backend openai
        obscura chat "test" --tools off --no-stream
        obscura chat "test" --tool-policy required:search
        obscura chat --backend copilot --list-sessions
        obscura chat "test" --backend copilot --model-alias copilot_automation_safe
    """
    import asyncio

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    async def _run_chat() -> None:
        from obscura.core.client import ObscuraClient
        from obscura.core.types import (
            AgentEventKind,
            SessionRef,
            Backend as BackendEnum,
            ToolChoice,
        )

        # --- Step 4: Memory context injection ---
        effective_system = system_prompt
        cli_user: Any = None
        if memory_enabled and bool(prompt and prompt.strip()):
            try:
                cli_user = _resolve_cli_user()
                ctx = _load_memory_context(cli_user, prompt or "")
                if ctx:
                    effective_system = (
                        f"{system_prompt}\n\n[Relevant context from memory]\n{ctx}"
                        if system_prompt
                        else f"[Relevant context from memory]\n{ctx}"
                    )
            except Exception:
                pass  # memory injection is best-effort

        # --- Steps 1-2: Resolve + start backend ---
        async with ObscuraClient(
            backend,
            model=model,
            model_alias=model_alias,
            automation_safe=automation_safe,
            system_prompt=effective_system,
            permission_mode=permission_mode,
            cwd=cwd,
        ) as client:
            # --- List sessions mode ---
            if list_sessions:
                sessions = await client.list_sessions()
                if not sessions:
                    console.print("[yellow]No sessions found.[/]")
                else:
                    for s in sessions:
                        console.print(f"  {s.session_id}  ({s.backend.value})")
                return

            if tools == "on":
                from obscura.tools.system import get_system_tool_specs

                for tool_spec in get_system_tool_specs():
                    client.register_tool(tool_spec)

            # --- Step 3: Resolve/create session ---
            session_ref: SessionRef | None = None
            if session:
                ref = SessionRef(session_id=session, backend=BackendEnum(backend))
                await client.resume_session(ref)
                session_ref = ref
            else:
                try:
                    session_ref = await client.create_session()
                except Exception:
                    pass  # session creation is optional

            # --- Step 5: Build kwargs ---
            loop_kwargs: dict[str, Any] = {}
            if tools == "off":
                loop_kwargs["tool_choice"] = ToolChoice.none()
            else:
                tc = _parse_tool_policy(tool_policy)
                if tc is not None:
                    loop_kwargs["tool_choice"] = tc

            # Transcript collection
            transcript: list[dict[str, str]] = []

            # --- Step 6: Route unified vs native ---
            if mode == "native":
                await _run_native(
                    client,
                    backend,
                    model,
                    prompt,
                    no_stream,
                    json_out,
                    interactive,
                    transcript,
                )
            elif interactive:
                # --- Unified interactive mode ---
                console.print(
                    f"[bold green]Obscura chat[/] ({backend}"
                    f"{', ' + model if model else ''})"
                )
                console.print("[dim]Type 'exit' or Ctrl-C to quit.[/]\n")

                while True:
                    try:
                        user_input: str = click.prompt("You", type=str)
                    except (EOFError, click.Abort):
                        break
                    if user_input.strip().lower() in ("exit", "quit"):
                        break

                    transcript.append({"role": "user", "content": user_input})
                    console.print("[bold cyan]Assistant:[/] ", end="")

                    turn_text = ""
                    async for event in client.run_loop(
                        user_input, max_turns=max_turns, **loop_kwargs
                    ):
                        _render_event(event, show_aux=False, show_thinking=False)
                        if event.kind == AgentEventKind.TEXT_DELTA:
                            turn_text += event.text
                    console.print()
                    transcript.append({"role": "assistant", "content": turn_text})

            elif prompt:
                # --- Unified single-shot ---
                transcript.append({"role": "user", "content": prompt})

                if no_stream:
                    msg = await client.send(prompt, **loop_kwargs)
                    if json_out:
                        console.print_json(json.dumps({"text": msg.text}))
                    else:
                        console.print(msg.text)
                    transcript.append({"role": "assistant", "content": msg.text})
                else:
                    turn_text = ""
                    async for event in client.run_loop(
                        prompt, max_turns=max_turns, **loop_kwargs
                    ):
                        _render_event(event)
                        if event.kind == AgentEventKind.TEXT_DELTA:
                            turn_text += event.text
                    console.print()
                    transcript.append({"role": "assistant", "content": turn_text})
            else:
                console.print("[yellow]Provide a prompt or use --interactive mode.[/]")
                return

            # --- Steps 8-9: Persist transcript + update memory ---
            if memory_enabled and transcript and cli_user is not None:
                sid = session_ref.session_id if session_ref else "anonymous"
                _persist_transcript(cli_user, sid, transcript, backend)

                # Store last session metadata
                try:
                    import time as _time

                    from obscura.memory import MemoryStore

                    mem = MemoryStore.for_user(cli_user)
                    last_text = transcript[-1].get("content", "")
                    mem.set(
                        "last_session",
                        {
                            "session_id": sid,
                            "backend": backend,
                            "model": model,
                            "timestamp": int(_time.time()),
                            "summary": last_text[:500],
                        },
                        namespace="session",
                    )
                except Exception:
                    pass

    async def _run_native(
        client: Any,
        backend: str,
        model: str | None,
        prompt: str | None,
        no_stream: bool,
        json_out: bool,
        interactive: bool,
        transcript: list[dict[str, str]],
    ) -> None:
        """Native mode: bypass agent loop, use raw SDK handle."""
        handle = client.native
        if handle.client is None:
            console.print("[red]Native client not available for this backend.[/]")
            return

        console.print(f"[dim]Native mode: raw {backend} SDK[/]")

        if not prompt and not interactive:
            console.print("[yellow]Provide a prompt or use --interactive mode.[/]")
            return

        if backend in ("openai", "moonshot", "localllm"):
            # AsyncOpenAI-compatible client
            actual_model = model or "gpt-4o"

            async def _native_openai_turn(user_msg: str) -> str:
                transcript.append({"role": "user", "content": user_msg})
                if no_stream:
                    resp = await handle.client.chat.completions.create(
                        model=actual_model,
                        messages=[{"role": "user", "content": user_msg}],
                        stream=False,
                    )
                    text: str = resp.choices[0].message.content or ""
                    if json_out:
                        console.print_json(json.dumps({"text": text}))
                    else:
                        console.print(text)
                    transcript.append({"role": "assistant", "content": text})
                    return text
                else:
                    resp_stream = await handle.client.chat.completions.create(
                        model=actual_model,
                        messages=[{"role": "user", "content": user_msg}],
                        stream=True,
                    )
                    parts: list[str] = []
                    async for chunk in resp_stream:
                        delta = chunk.choices[0].delta
                        if delta.content:
                            console.print(delta.content, end="")
                            parts.append(delta.content)
                    console.print()
                    text = "".join(parts)
                    transcript.append({"role": "assistant", "content": text})
                    return text

            if interactive:
                console.print(
                    f"[bold green]Obscura native[/] ({backend}, {actual_model})"
                )
                console.print("[dim]Type 'exit' or Ctrl-C to quit.[/]\n")
                while True:
                    try:
                        user_input = click.prompt("You", type=str)
                    except (EOFError, click.Abort):
                        break
                    if user_input.strip().lower() in ("exit", "quit"):
                        break
                    console.print("[bold cyan]Assistant:[/] ", end="")
                    await _native_openai_turn(user_input)
            elif prompt:
                await _native_openai_turn(prompt)

        else:
            # Claude / Copilot native — fallback to unified send()
            console.print(
                f"[dim]Native mode for {backend}: using backend.send() "
                f"(raw SDK interactive mode not yet wired)[/]"
            )
            if prompt:
                transcript.append({"role": "user", "content": prompt})
                msg = await client.send(prompt)
                console.print(msg.text)
                transcript.append({"role": "assistant", "content": msg.text})

    try:
        asyncio.run(_run_chat())
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/]")


# ---------------------------------------------------------------------------
# Passthrough command — delegates to a vendor CLI
# ---------------------------------------------------------------------------


@cli.command("passthrough", context_settings={"ignore_unknown_options": True})
@click.argument("vendor", type=click.Choice(["claude", "openai", "copilot"]))
@click.argument("vendor_args", nargs=-1, type=click.UNPROCESSED)
@click.option(
    "--capture/--no-capture",
    default=False,
    help="Capture transcript (disables interactive mode).",
)
def passthrough(vendor: str, vendor_args: tuple[str, ...], capture: bool) -> None:
    """Run a vendor CLI, capturing transcript for memory.

    \b
    Interactive (default) — opens vendor CLI with full terminal access:
        obscura passthrough copilot
        obscura passthrough claude

    \b
    With args — still interactive, passes args through:
        obscura passthrough copilot -- -p "hello"

    \b
    Captured mode — pipes output for transcript storage:
        obscura passthrough --capture copilot -- -p "hello"
    """
    import shutil
    import subprocess

    vendor_cmds: dict[str, str] = {
        "claude": "claude",
        "openai": "codex",
        "copilot": "copilot",
    }

    cmd_name = vendor_cmds[vendor]
    cmd_path: str | None = shutil.which(cmd_name)
    if cmd_path is None:
        console.print(
            f"[bold red]Error:[/] '{cmd_name}' CLI not found on PATH. Install it first."
        )
        sys.exit(1)

    resolved = cmd_path or cmd_name
    full_cmd: list[str] = [resolved, *vendor_args]

    # --capture: pipe stdout/stderr for transcript storage
    if capture:
        import asyncio

        async def _run_captured() -> None:
            import time

            console.print(f"[dim]Running (captured): {' '.join(full_cmd)}[/]\n")

            proc = await asyncio.create_subprocess_exec(
                *full_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            transcript_lines: list[str] = []

            async def _stream_output(
                stream: asyncio.StreamReader | None, is_err: bool = False
            ) -> None:
                if stream is None:
                    return
                while True:
                    line_bytes: bytes = await stream.readline()
                    if not line_bytes:
                        break
                    line: str = line_bytes.decode("utf-8", errors="replace")
                    transcript_lines.append(line)
                    if is_err:
                        console.print(f"[red]{line}[/]", end="")
                    else:
                        console.print(line, end="")

            await asyncio.gather(
                _stream_output(proc.stdout),
                _stream_output(proc.stderr, is_err=True),
            )

            await proc.wait()
            console.print(f"\n[dim]Process exited with code {proc.returncode}[/]")

            # Session ID for tracking
            ts = int(time.time())
            session_id = f"passthrough_{vendor}_{ts}"

            # Persist transcript to file (best-effort)
            if transcript_lines:
                try:
                    from pathlib import Path

                    transcript_dir = Path.home() / ".obscura" / "transcripts"
                    transcript_dir.mkdir(parents=True, exist_ok=True)
                    path = transcript_dir / f"{session_id}.txt"
                    path.write_text("".join(transcript_lines)[:50000])
                    console.print(f"[dim]Transcript saved to {path}[/]")
                except Exception:
                    pass

            # Persist to MemoryStore (best-effort)
            if transcript_lines:
                try:
                    from obscura.memory import MemoryStore

                    cli_user = _resolve_cli_user()
                    mem = MemoryStore.for_user(cli_user)
                    mem.set(
                        f"passthrough:{session_id}",
                        {
                            "vendor": vendor,
                            "command": " ".join(full_cmd),
                            "transcript": "".join(transcript_lines)[:50000],
                            "exit_code": proc.returncode,
                            "timestamp": ts,
                        },
                        namespace="passthrough",
                    )
                    console.print(f"[dim]Transcript stored in memory ({session_id})[/]")
                except Exception:
                    pass

        try:
            asyncio.run(_run_captured())
        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled.[/]")
        return

    # Default: interactive mode — hand off the terminal directly
    console.print(f"[dim]Launching: {' '.join(full_cmd)}[/]\n")
    try:
        result = subprocess.run(full_cmd)
        sys.exit(result.returncode)
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/]")


# ---------------------------------------------------------------------------
# Telemetry helpers (no-op when dependencies are unavailable)
# ---------------------------------------------------------------------------


def _init_cli_telemetry() -> None:
    """Initialize telemetry for CLI mode with text logging."""
    try:
        from obscura.core.config import ObscuraConfig
        from obscura.telemetry import init_telemetry

        config = ObscuraConfig.from_env()
        config.log_format = "text"
        init_telemetry(config)
    except Exception:
        pass


class _StderrLogger:
    """Minimal fallback logger that writes to stderr."""

    def info(self, event: str, **kw: Any) -> None:
        msg = kw.get("msg", event)
        print(msg, file=sys.stderr)

    def error(self, event: str, **kw: Any) -> None:
        msg = kw.get("error", kw.get("msg", event))
        print(f"Error: {msg}", file=sys.stderr)

    def warning(self, event: str, **kw: Any) -> None:
        msg = kw.get("msg", event)
        print(f"Warning: {msg}", file=sys.stderr)


def _get_cli_logger(name: str) -> Any:
    """Return a structlog logger, or a stderr fallback."""
    try:
        from obscura.telemetry.logging import get_logger

        return get_logger(name)
    except Exception:
        return _StderrLogger()


def _summarize_tool_input(raw_json: str) -> str:
    """Return a concise one-line summary of tool input."""
    text = raw_json.strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            parsed_dict = cast(dict[Any, Any], parsed)
            keys = [str(key) for key in parsed_dict.keys()]
            preview = ", ".join(keys[:4])
            if len(keys) > 4:
                preview = f"{preview}, ..."
            return f"args keys: {preview}" if preview else "args: {}"
        if isinstance(parsed, list):
            parsed_list = cast(list[Any], parsed)
            return f"args list(len={len(parsed_list)})"
        scalar = str(parsed)
    except Exception:
        scalar = text
    scalar = scalar.replace("\n", " ").strip()
    if len(scalar) > 120:
        scalar = f"{scalar[:117]}..."
    return f"args: {scalar}"


# ---------------------------------------------------------------------------
# Observe command — agent state monitoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservedAgentState:
    """Compact runtime state object used by the observe command."""

    agent_id: str
    name: str
    status: str
    updated_at: datetime
    iteration_count: int
    error_message: str | None

    def signature(self) -> tuple[str, str, int, str | None]:
        return (
            self.status,
            self.updated_at.isoformat(),
            self.iteration_count,
            self.error_message,
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ObservedAgentState | None:
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            return None
        name = payload.get("name")
        status = payload.get("status")
        updated_raw = payload.get("updated_at")
        if not isinstance(name, str) or not isinstance(status, str):
            return None
        if not isinstance(updated_raw, str):
            return None
        updated_at = _parse_iso_datetime(updated_raw)
        if updated_at is None:
            return None
        iteration_raw = payload.get("iteration_count", 0)
        iteration_count = int(iteration_raw) if isinstance(iteration_raw, int) else 0
        error_message = payload.get("error_message")
        if error_message is not None and not isinstance(error_message, str):
            error_message = str(error_message)
        return cls(
            agent_id=agent_id,
            name=name,
            status=status,
            updated_at=updated_at,
            iteration_count=iteration_count,
            error_message=error_message,
        )


def _parse_iso_datetime(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def collect_observed_agent_states(
    store: Any,
    *,
    namespace: str,
) -> list[ObservedAgentState]:
    states: list[ObservedAgentState] = []
    for key in store.list_keys(namespace=namespace):
        if not key.key.startswith("agent_state_"):
            continue
        payload = store.get(key.key, namespace=namespace)
        if not isinstance(payload, dict):
            continue
        payload_dict = cast(dict[Any, Any], payload)
        typed_payload: dict[str, Any] = {}
        for raw_key, raw_value in payload_dict.items():
            key_name = raw_key if isinstance(raw_key, str) else str(raw_key)
            typed_payload[key_name] = raw_value
        state = ObservedAgentState.from_payload(typed_payload)
        if state is None:
            continue
        states.append(state)
    return sorted(states, key=lambda entry: (entry.updated_at, entry.agent_id))


def find_stale_agent_ids(
    states: list[ObservedAgentState],
    *,
    now: datetime,
    stale_seconds: float,
) -> list[str]:
    stale: list[str] = []
    for state in states:
        if state.status not in {"RUNNING", "WAITING"}:
            continue
        age = (now - state.updated_at).total_seconds()
        if age >= stale_seconds:
            stale.append(state.agent_id)
    return stale


def _render_state_line(state: ObservedAgentState) -> str:
    updated = state.updated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    base = (
        f"{state.agent_id} name={state.name} status={state.status} "
        f"iter={state.iteration_count} updated={updated}"
    )
    if state.error_message:
        return f"{base} error={state.error_message}"
    return base


def run_observe(
    user_id: str | argparse.Namespace | None = None,
    email: str = "observe@obscura.local",
    org_id: str = "org-observe",
    namespace: str = "agent:runtime",
    interval_seconds: float = 1.0,
    stale_seconds: float = 20.0,
    duration_seconds: float = 0.0,
    once: bool = False,
) -> int:
    """Core observe logic, callable from both click and argparse interfaces."""
    import time

    from obscura.auth.models import AuthenticatedUser
    from obscura.memory import MemoryStore

    # Support legacy argparse.Namespace usage: run_observe(args)
    if isinstance(user_id, argparse.Namespace):
        args = user_id
        user_id = str(args.user_id)
        email = str(getattr(args, "email", email))
        org_id = str(getattr(args, "org_id", org_id))
        namespace = str(getattr(args, "namespace", namespace))
        interval_seconds = float(getattr(args, "interval_seconds", interval_seconds))
        stale_seconds = float(getattr(args, "stale_seconds", stale_seconds))
        duration_seconds = float(getattr(args, "duration_seconds", duration_seconds))
        once = bool(getattr(args, "once", once))

    if user_id is None:
        print("Error: user_id is required", file=sys.stderr)
        return 1

    interval = max(0.1, interval_seconds)
    stale_threshold = max(1.0, stale_seconds)
    max_duration = max(0.0, duration_seconds)

    user = AuthenticatedUser(
        user_id=user_id,
        email=email,
        roles=("operator",),
        org_id=org_id,
        token_type="user",
        raw_token="observe-token",
    )
    store = MemoryStore.for_user(user)
    stats = store.get_stats()
    db_path = str(stats.get("db_path", "unknown"))
    print(
        f"[observe] user={user.user_id} namespace={namespace} db={db_path} "
        f"interval={interval:.1f}s stale={stale_threshold:.1f}s",
        flush=True,
    )

    previous_by_id: dict[str, tuple[str, str, int, str | None]] = {}
    stale_alerts: set[tuple[str, str]] = set()
    started = time.monotonic()

    try:
        while True:
            now = datetime.now(UTC)
            states = collect_observed_agent_states(store, namespace=namespace)
            current_ids = {state.agent_id for state in states}

            for state in states:
                signature = state.signature()
                if previous_by_id.get(state.agent_id) != signature:
                    print(_render_state_line(state), flush=True)

            stale_ids = set(
                find_stale_agent_ids(
                    states, now=now, stale_seconds=stale_threshold
                )
            )
            for state in states:
                if state.agent_id not in stale_ids:
                    continue
                signature_key = (state.agent_id, state.updated_at.isoformat())
                if signature_key in stale_alerts:
                    continue
                age = (now - state.updated_at).total_seconds()
                print(
                    f"WARNING: stalled agent {state.agent_id} "
                    f"(status={state.status}, age={age:.1f}s)",
                    flush=True,
                )
                stale_alerts.add(signature_key)

            removed_ids = set(previous_by_id) - current_ids
            for agent_id in sorted(removed_ids):
                print(
                    f"{agent_id} removed from namespace={namespace}", flush=True
                )

            previous_by_id = {
                state.agent_id: state.signature() for state in states
            }

            if once:
                break
            if max_duration > 0 and (time.monotonic() - started) >= max_duration:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        return 130

    return 0


@cli.command("observe")
@click.option("--user-id", required=True, help="User ID to observe")
@click.option(
    "--email",
    default="observe@obscura.local",
    help="Email for observer identity",
)
@click.option("--org-id", default="org-observe", help="Org ID for observer identity")
@click.option(
    "--namespace",
    default="agent:runtime",
    help="Memory namespace containing agent state records",
)
@click.option(
    "--interval-seconds",
    default=1.0,
    type=float,
    help="Polling interval in seconds",
)
@click.option(
    "--stale-seconds",
    default=20.0,
    type=float,
    help="Stalled warning threshold in seconds",
)
@click.option(
    "--duration-seconds",
    default=0.0,
    type=float,
    help="Max observe duration (0 = run until interrupted)",
)
@click.option("--once", is_flag=True, help="Print one snapshot and exit")
def observe_cmd(
    user_id: str,
    email: str,
    org_id: str,
    namespace: str,
    interval_seconds: float,
    stale_seconds: float,
    duration_seconds: float,
    once: bool,
) -> None:
    """Tail agent runtime state and highlight stalled agents."""
    code = run_observe(
        user_id=user_id,
        email=email,
        org_id=org_id,
        namespace=namespace,
        interval_seconds=interval_seconds,
        stale_seconds=stale_seconds,
        duration_seconds=duration_seconds,
        once=once,
    )
    if code:
        sys.exit(code)


# ---------------------------------------------------------------------------
# Backward-compat: argparse parser for legacy test imports
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Backward-compatible argparse parser for legacy test imports."""
    p = argparse.ArgumentParser(prog="obscura")
    sub = p.add_subparsers(dest="command", help="Available commands")

    observe_parser = sub.add_parser("observe", help="Observe agent state")
    observe_parser.add_argument("--user-id", required=True)
    observe_parser.add_argument("--email", default="observe@obscura.local")
    observe_parser.add_argument("--org-id", default="org-observe")
    observe_parser.add_argument("--namespace", default="agent:runtime")
    observe_parser.add_argument("--interval-seconds", type=float, default=1.0)
    observe_parser.add_argument("--stale-seconds", type=float, default=20.0)
    observe_parser.add_argument("--duration-seconds", type=float, default=0.0)
    observe_parser.add_argument("--once", action="store_true")

    return p


# Main entry point
def main() -> None:
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
