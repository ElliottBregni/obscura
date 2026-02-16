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

import json
import os
import sys
from collections.abc import Generator
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
        "sdk.server:create_app",
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
# Chat command (owned mode — direct backend, no server needed)
# ---------------------------------------------------------------------------


@cli.command("chat")
@click.argument("prompt", required=False)
@click.option(
    "--backend",
    "-b",
    default="openai",
    type=click.Choice(["openai", "claude", "copilot", "localllm"]),
    help="Backend to use",
)
@click.option("--model", "-m", default=None, help="Model ID override")
@click.option("--system-prompt", "-s", default="", help="System instructions")
@click.option("--session", default=None, help="Session ID to resume")
@click.option("--no-stream", is_flag=True, help="Disable streaming (wait for full response)")
@click.option("--json-output", "json_out", is_flag=True, help="Output as JSON")
@click.option("--interactive", "-i", is_flag=True, help="Interactive multi-turn mode")
@click.option("--max-turns", default=10, help="Max agent loop turns")
def chat(
    prompt: str | None,
    backend: str,
    model: str | None,
    system_prompt: str,
    session: str | None,
    no_stream: bool,
    json_out: bool,
    interactive: bool,
    max_turns: int,
) -> None:
    """Chat directly with a backend (no server required).

    \b
    Examples:
        obscura chat "explain this code" --backend openai
        obscura chat --backend claude --interactive
        obscura chat "hello" --backend localllm --no-stream
    """
    import asyncio

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    async def _run_chat() -> None:
        from sdk.client import ObscuraClient
        from sdk.internal.types import AgentEventKind

        async with ObscuraClient(
            backend,
            model=model,
            system_prompt=system_prompt,
        ) as client:
            # Resume session if provided
            if session:
                from sdk.internal.types import SessionRef, Backend as BackendEnum

                ref = SessionRef(session_id=session, backend=BackendEnum(backend))
                await client.resume_session(ref)

            if interactive:
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

                    console.print("[bold cyan]Assistant:[/] ", end="")
                    async for event in client.run_loop(
                        user_input, max_turns=max_turns
                    ):
                        if event.kind == AgentEventKind.TEXT_DELTA:
                            console.print(event.text, end="")
                        elif event.kind == AgentEventKind.TOOL_CALL:
                            console.print(
                                f"\n[dim][tool] {event.tool_name}[/]", end=""
                            )
                        elif event.kind == AgentEventKind.TOOL_RESULT:
                            console.print(
                                f"\n[dim][result] {event.tool_result[:80]}[/]",
                                end="",
                            )
                    console.print()

            elif prompt:
                if no_stream:
                    msg = await client.send(prompt)
                    if json_out:
                        console.print_json(json.dumps({"text": msg.text}))
                    else:
                        console.print(msg.text)
                else:
                    async for event in client.run_loop(
                        prompt, max_turns=max_turns
                    ):
                        if event.kind == AgentEventKind.TEXT_DELTA:
                            console.print(event.text, end="")
                        elif event.kind == AgentEventKind.TOOL_CALL:
                            console.print(
                                f"\n[dim][tool] {event.tool_name}[/]", end=""
                            )
                        elif event.kind == AgentEventKind.TOOL_RESULT:
                            console.print(
                                f"\n[dim][result] {event.tool_result[:80]}[/]",
                                end="",
                            )
                    console.print()
            else:
                console.print(
                    "[yellow]Provide a prompt or use --interactive mode.[/]"
                )

    try:
        asyncio.run(_run_chat())
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/]")


# ---------------------------------------------------------------------------
# Passthrough command — delegates to a vendor CLI
# ---------------------------------------------------------------------------


@cli.command("passthrough", context_settings={"ignore_unknown_options": True})
@click.argument("vendor", type=click.Choice(["claude", "openai"]))
@click.argument("vendor_args", nargs=-1, type=click.UNPROCESSED)
def passthrough(vendor: str, vendor_args: tuple[str, ...]) -> None:
    """Run a vendor CLI, capturing transcript for memory.

    \b
    Examples:
        obscura passthrough claude -- chat --model sonnet
        obscura passthrough openai -- api chat.completions.create -m gpt-4o
    """
    import asyncio
    import shutil

    vendor_cmds: dict[str, str] = {
        "claude": "claude",
        "openai": "openai",
    }

    cmd_name = vendor_cmds[vendor]
    cmd_path: str | None = shutil.which(cmd_name)
    if cmd_path is None:
        console.print(
            f"[bold red]Error:[/] '{cmd_name}' CLI not found on PATH. "
            f"Install it first."
        )
        sys.exit(1)

    async def _run_passthrough() -> None:
        full_cmd: list[str] = [cmd_path or cmd_name, *vendor_args]
        console.print(
            f"[dim]Running: {' '.join(full_cmd)}[/]\n"
        )

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

        # Persist transcript to file (best-effort)
        if transcript_lines:
            try:
                import time
                from pathlib import Path

                transcript_dir = Path.home() / ".obscura" / "transcripts"
                transcript_dir.mkdir(parents=True, exist_ok=True)
                ts = int(time.time())
                path = transcript_dir / f"passthrough_{vendor}_{ts}.txt"
                path.write_text("".join(transcript_lines)[:50000])
                console.print(f"[dim]Transcript saved to {path}[/]")
            except Exception:
                pass  # transcript persistence is best-effort

    try:
        asyncio.run(_run_passthrough())
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/]")


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
