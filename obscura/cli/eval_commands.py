"""CLI commands for the Obscura eval framework."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
from rich.console import Console

console = Console()


@click.group("eval")
def eval_group() -> None:
    """Eval framework for measuring tool calling and response quality."""


@eval_group.command("run")
@click.argument("suite_paths", nargs=-1, type=click.Path(exists=True, path_type=Path))
@click.option("-b", "--backend", default=None, help="Backend override.")
@click.option("-m", "--model", default=None, help="Model override.")
@click.option("--tag", multiple=True, help="Filter cases by tag.")
@click.option("--case", "case_id", default=None, help="Run a specific case by ID.")
@click.option("--set-baseline", is_flag=True, default=False, help="Promote results as new baseline.")
@click.option(
    "--judge-backend", default=None,
    help="Backend for LLM-as-judge (defaults to eval backend).",
)
@click.option(
    "--format", "output_format", default="table",
    type=click.Choice(["table", "json", "md"]),
    help="Output format.",
)
def run_cmd(
    suite_paths: tuple[Path, ...],
    backend: str | None,
    model: str | None,
    tag: tuple[str, ...],
    case_id: str | None,
    set_baseline: bool,
    judge_backend: str | None,
    output_format: str,
) -> None:
    """Run eval suites and report results."""
    from obscura.eval.compiler import compile_suite
    from obscura.eval.loader import load_all_eval_suites, load_eval_suite
    from obscura.eval.models import CompiledEvalCase
    from obscura.eval.report import render_json, render_markdown, render_table

    # Load suites
    if suite_paths:
        specs = [load_eval_suite(p) for p in suite_paths]
    else:
        specs = load_all_eval_suites()

    if not specs:
        console.print("[yellow]No eval suites found.[/yellow]")
        sys.exit(1)

    all_cases: list[CompiledEvalCase] = []
    suite_id = ""
    for spec in specs:
        cases = compile_suite(spec)
        suite_id = spec.meta.id
        for case in cases:
            # Apply overrides
            if backend or model:
                case = CompiledEvalCase(
                    id=case.id,
                    title=case.title,
                    prompt=case.prompt,
                    suite_id=case.suite_id,
                    backend=backend or case.backend,
                    model=model or case.model,
                    max_turns=case.max_turns,
                    tool_mode=case.tool_mode,
                    fixtures_dir=case.fixtures_dir,
                    golden_session_id=case.golden_session_id,
                    tags=case.tags,
                    expect_tool_calls=case.expect_tool_calls,
                    assertions=case.assertions,
                    judge_criteria=case.judge_criteria,
                    judge_rubric=case.judge_rubric,
                    judge_pass_threshold=case.judge_pass_threshold,
                    regression_baseline_run_id=case.regression_baseline_run_id,
                    regression_score_threshold=case.regression_score_threshold,
                    regression_max_score_delta=case.regression_max_score_delta,
                )

            # Filter by tag
            if tag and not any(t in case.tags for t in tag):
                continue

            # Filter by case ID
            if case_id and case.id != case_id:
                continue

            all_cases.append(case)

    if not all_cases:
        console.print("[yellow]No eval cases matched filters.[/yellow]")
        sys.exit(1)

    console.print(f"Running {len(all_cases)} eval case(s)...")

    async def _run() -> None:
        from obscura.eval.engine import EvalEngine
        from obscura.eval.store import EvalResultStore

        # For now, create a minimal mock backend for dry-run
        # Real usage will resolve backend from the provider factory
        resolved_backend = _resolve_backend(all_cases[0].backend, all_cases[0].model)
        resolved_judge = (
            _resolve_backend(judge_backend, all_cases[0].model)
            if judge_backend
            else None
        )

        store = EvalResultStore()
        engine = EvalEngine(
            backend=resolved_backend,
            tool_registry=_resolve_tool_registry(),
            judge_backend=resolved_judge,
            result_store=store,
        )

        summary = await engine.run_suite(tuple(all_cases), suite_id)

        if set_baseline:
            await store.promote_baseline(summary.run_id, suite_id)
            console.print(f"[green]Baseline set: {summary.run_id}[/green]")

        if output_format == "json":
            click.echo(render_json(summary))
        elif output_format == "md":
            click.echo(render_markdown(summary))
        else:
            render_table(summary, console=console)

    asyncio.run(_run())


@eval_group.command("list")
@click.option("--suites", is_flag=True, help="List available eval suites.")
@click.option("--runs", is_flag=True, help="List recent eval runs.")
@click.option("--baselines", is_flag=True, help="List current baselines.")
def list_cmd(suites: bool, runs: bool, baselines: bool) -> None:
    """List eval suites, runs, or baselines."""
    if suites or (not runs and not baselines):
        from obscura.eval.loader import discover_eval_files

        files = discover_eval_files()
        if not files:
            console.print("[yellow]No eval suites found.[/yellow]")
            return
        console.print(f"[bold]Found {len(files)} eval suite(s):[/bold]")
        for f in files:
            console.print(f"  {f}")

    if runs:

        async def _list_runs() -> None:
            from obscura.eval.store import EvalResultStore

            store = EvalResultStore()
            results = await store.list_runs()
            if not results:
                console.print("[yellow]No eval runs found.[/yellow]")
                return
            console.print(f"[bold]Recent eval runs ({len(results)}):[/bold]")
            for r in results:
                console.print(
                    f"  {r['run_id']}  {r['suite_id']}  "
                    f"{r['passed']}/{r['total_cases']}P  "
                    f"avg={r['avg_composite_score']:.2f}  "
                    f"{r['created_at']}"
                )

        asyncio.run(_list_runs())

    if baselines:

        async def _list_baselines() -> None:
            from obscura.eval.store import EvalResultStore

            store = EvalResultStore()
            results = await store.list_baselines()
            if not results:
                console.print("[yellow]No baselines found.[/yellow]")
                return
            console.print(f"[bold]Current baselines ({len(results)}):[/bold]")
            for r in results:
                console.print(
                    f"  {r['case_id']}  suite={r['suite_id']}  "
                    f"run={r['run_id']}  score={r['score']:.2f}"
                )

        asyncio.run(_list_baselines())


@eval_group.command("report")
@click.option("--run", "run_id", default=None, help="Show report for a specific run ID.")
@click.option("--suite", "suite_id", default=None, help="Show latest run for a suite.")
@click.option(
    "--format", "output_format", default="table",
    type=click.Choice(["table", "json", "md"]),
    help="Output format.",
)
def report_cmd(run_id: str | None, suite_id: str | None, output_format: str) -> None:
    """Show eval run reports."""
    console.print("[yellow]Report retrieval from stored runs not yet implemented.[/yellow]")
    console.print("Use 'obscura eval run' to execute evals with --format option.")


# ---------------------------------------------------------------------------
# Backend / registry helpers (placeholder wiring)
# ---------------------------------------------------------------------------


def _resolve_backend(backend_name: str, model_name: str) -> object:
    """Resolve a BackendProtocol instance by name."""
    from obscura.core.auth import resolve_auth
    from obscura.core.types import Backend

    try:
        backend_enum = Backend(backend_name)
    except ValueError:
        raise click.ClickException(
            f"Unknown backend '{backend_name}'. "
            "Available: claude, copilot, openai, localllm, moonshot"
        )

    auth = resolve_auth(backend_enum)

    if backend_enum == Backend.CLAUDE:
        import os
        import shutil

        api_key = auth.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")

        if api_key:
            from obscura.eval.eval_backend import AnthropicEvalBackend

            return AnthropicEvalBackend(api_key=api_key, model=model_name)

        # Fall back to the Claude CLI which handles OAuth internally
        if shutil.which("claude"):
            from obscura.eval.eval_backend import ClaudeCliEvalBackend

            return ClaudeCliEvalBackend(model=model_name)

        raise click.ClickException(
            "No Anthropic API key found and 'claude' CLI not on PATH. "
            "Set ANTHROPIC_API_KEY or install Claude Code."
        )

    if backend_enum == Backend.COPILOT:
        from obscura.providers.copilot import CopilotBackend

        return CopilotBackend(auth)

    if backend_enum == Backend.OPENAI:
        from obscura.providers.openai import OpenAIBackend

        return OpenAIBackend(auth, model=model_name)

    if backend_enum == Backend.LOCALLLM:
        from obscura.providers.localllm import LocalLLMBackend

        return LocalLLMBackend(auth, model=model_name)

    if backend_enum == Backend.MOONSHOT:
        from obscura.providers.moonshot import MoonshotBackend

        return MoonshotBackend(auth, model=model_name)

    raise click.ClickException(f"Backend '{backend_name}' not yet supported for evals.")


def _resolve_tool_registry() -> object:
    """Get a ToolRegistry with standard system tools registered."""
    from obscura.core.tools import ToolRegistry

    registry = ToolRegistry()
    try:
        from obscura.tools.system import get_system_tool_specs

        for spec in get_system_tool_specs():
            registry.register(spec)
    except Exception:
        pass
    return registry
