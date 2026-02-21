"""Run a live backend agent demo that exercises system tools."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator, Sequence

from sdk.demo.framework import (
    DemoAgentConfig,
    demo_agent_session,
    required_args_tool_guard,
)
from sdk.internal.types import AgentEvent, AgentEventKind


def _role_for_backend(backend: str) -> str:
    return f"agent:{backend}"


def default_prompt() -> str:
    return (
        "Use system tools to inspect this machine. For Malware "
        "Then return a short summary."
        "What can be done to harden system?"
    )


async def _collect_events(
    events: AsyncIterator[AgentEvent],
    *,
    show_events: bool,
) -> tuple[str, list[str]]:
    text_parts: list[str] = []
    tool_calls: list[str] = []
    async for event in events:
        if event.kind == AgentEventKind.TEXT_DELTA:
            text_parts.append(event.text)
            if show_events and event.text:
                print(event.text, end="", flush=True)
            continue
        if event.kind == AgentEventKind.TOOL_CALL:
            call_desc = f"{event.tool_name}({event.tool_input})"
            tool_calls.append(call_desc)
            if show_events:
                print(f"\n[tool_call] {call_desc}")
            continue
        if event.kind == AgentEventKind.TOOL_RESULT and show_events:
            status = "error" if event.is_error else "ok"
            print(f"[tool_result:{status}] {event.tool_name}")
    if show_events:
        print()
    return "".join(text_parts), tool_calls


async def run_live_system_tools_demo(
    *,
    backend: str,
    prompt: str,
    max_turns: int = 8,
    show_events: bool = True,
    start_timeout_seconds: float = 20.0,
    run_timeout_seconds: float = 180.0,
) -> tuple[str, list[str]]:
    config = DemoAgentConfig(
        name=f"{backend}-system-tools-live-demo",
        model=backend,
        role=_role_for_backend(backend),
        system_prompt=(
            "You are a system tools demo agent. "
            "Always include required tool arguments and use tools whenever possible."
        ),
        memory_namespace=f"demo:system-tools:{backend}",
        enable_system_tools=True,
    )
    async with demo_agent_session(
        config,
        start_timeout_seconds=start_timeout_seconds,
    ) as agent:
        on_confirm = required_args_tool_guard(agent)
        result_text, tool_calls = await asyncio.wait_for(
            _collect_events(
                agent.stream_loop(prompt, max_turns=max_turns, on_confirm=on_confirm),
                show_events=show_events,
            ),
            timeout=run_timeout_seconds,
        )
        return result_text, tool_calls


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run live backend agent demo with system tools."
    )
    parser.add_argument(
        "--backend",
        default="copilot",
        choices=("copilot", "claude", "openai", "moonshot", "localllm"),
        help="Backend model to use.",
    )
    parser.add_argument(
        "--prompt",
        "-p",
        default=default_prompt(),
        help="Prompt to execute.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=8,
        help="Maximum APER loop turns.",
    )
    parser.add_argument(
        "--quiet-events",
        action="store_true",
        help="Hide streaming event logs; only print summary.",
    )
    parser.add_argument(
        "--start-timeout",
        type=float,
        default=20.0,
        help="Timeout (seconds) for runtime and agent startup.",
    )
    parser.add_argument(
        "--run-timeout",
        type=float,
        default=180.0,
        help="Timeout (seconds) for the APER run.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        text, tool_calls = asyncio.run(
            run_live_system_tools_demo(
                backend=str(args.backend),
                prompt=str(args.prompt),
                max_turns=int(args.max_turns),
                show_events=not bool(args.quiet_events),
                start_timeout_seconds=float(args.start_timeout),
                run_timeout_seconds=float(args.run_timeout),
            )
        )
    except TimeoutError as exc:
        print(f"Live system tools demo timeout: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:  # pragma: no cover - CLI guard
        print(f"Live system tools demo failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    print("\n=== Live System Tools Demo Summary ===")
    print(f"backend: {args.backend}")
    print(f"tool_calls: {len(tool_calls)}")
    if tool_calls:
        for item in tool_calls:
            print(f"- {item}")
    print("\nresponse:\n")
    print(text.strip())


if __name__ == "__main__":
    main()
