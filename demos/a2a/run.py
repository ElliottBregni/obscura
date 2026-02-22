"""
demos.a2a.run — CLI entry point for the A2A multi-agent demo.

Runs three A2A agent servers (Triage, Investigator, Resolution) in-process
using ASGITransport and orchestrates them via A2AClient protocol calls.

Usage::

    python -m demos.a2a.run --sample billing
    python -m demos.a2a.run --sample technical --mode streaming
    python -m demos.a2a.run --sample urgent --mode tool-adapter
    python -m demos.a2a.run --ticket "My API is broken (cust_003)"
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from httpx import ASGITransport

from demos.a2a.agents import (
    create_investigator_app,
    create_resolution_app,
    create_triage_app,
)
from demos.a2a.orchestrator import A2APipeline, A2AResult
from demos.support.run import SAMPLE_TICKETS
from obscura.integrations.a2a.types import TaskStatusUpdateEvent, TextPart


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _section(title: str, content: str) -> None:
    w = 72
    print(f"\n{'=' * w}")
    print(f"  {title}")
    print(f"{'=' * w}")
    print(content)


def _print_blocking_result(result: A2AResult) -> None:
    """Print a blocking pipeline result."""
    # Triage
    if result.triage_json:
        t = result.triage_json
        _section(
            "TRIAGE (A2A)",
            "\n".join(
                [
                    f"  Task ID:    {result.triage_task.id if result.triage_task else 'N/A'}",
                    f"  Category:   {t.get('category', '?')}",
                    f"  Severity:   {t.get('severity', '?')}",
                    f"  Urgency:    {t.get('urgency_detected', '?')}",
                    f"  Customer:   {t.get('customer_id', '?')}",
                    f"  Routing:    {t.get('routing', '?')}",
                ]
            ),
        )

    # Investigation
    if result.investigation_json:
        inv = result.investigation_json
        _section(
            "INVESTIGATION (A2A)",
            "\n".join(
                [
                    f"  Task ID:      {result.investigator_task.id if result.investigator_task else 'N/A'}",
                    f"  Root cause:   {str(inv.get('root_cause', '?'))[:80]}",
                    f"  Similar tix:  {len(inv.get('similar_tickets', []))}",
                    f"  KB articles:  {len(inv.get('kb_articles', []))}",
                    f"  Escalate:     {inv.get('should_escalate', '?')}",
                ]
            ),
        )

    # Resolution
    if result.resolution_json:
        res = result.resolution_json
        _section(
            "RESOLUTION (A2A)",
            "\n".join(
                [
                    f"  Task ID:  {result.resolution_task.id if result.resolution_task else 'N/A'}",
                    f"  Type:     {res.get('response_type', '?')}",
                    "",
                    "  Customer Message:",
                    "  " + res.get("customer_message", "N/A").replace("\n", "\n  "),
                ]
            ),
        )

    # Agent cards
    if result.agent_cards:
        cards_info: list[str] = []
        for name, card in result.agent_cards.items():
            skill_names = [s.name for s in card.skills]
            cards_info.append(
                f"  {name}: {card.name} (skills: {', '.join(skill_names)})"
            )
        _section("AGENT CARDS DISCOVERED", "\n".join(cards_info))

    # Audit
    _section(
        "PROTOCOL AUDIT",
        "\n".join(
            [
                f"  Mode:              {result.mode}",
                f"  Phases completed:  {result.phases}",
                f"  Total time:        {result.total_time_ms:.1f}ms",
                f"  Timestamp:         {result.timestamp}",
            ]
        ),
    )


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


async def run_blocking(ticket: str, verbose: bool = False) -> None:
    """Run the pipeline in blocking mode."""
    triage_app = create_triage_app()
    investigator_app = create_investigator_app()
    resolution_app = create_resolution_app()

    pipeline = A2APipeline(
        triage_transport=ASGITransport(app=triage_app),
        investigator_transport=ASGITransport(app=investigator_app),
        resolution_transport=ASGITransport(app=resolution_app),
    )

    result = await pipeline.run(ticket)
    _print_blocking_result(result)


async def run_streaming(ticket: str, verbose: bool = False) -> None:
    """Run the pipeline in streaming mode with real-time output."""
    triage_app = create_triage_app()
    investigator_app = create_investigator_app()
    resolution_app = create_resolution_app()

    pipeline = A2APipeline(
        triage_transport=ASGITransport(app=triage_app),
        investigator_transport=ASGITransport(app=investigator_app),
        resolution_transport=ASGITransport(app=resolution_app),
    )

    print(f"\n{'#' * 72}")
    print("  A2A STREAMING MODE")
    print(f"{'#' * 72}\n")

    current_agent = ""
    async for agent_name, event in pipeline.run_streaming(ticket):
        if agent_name != current_agent:
            current_agent = agent_name
            print(f"\n--- {agent_name.upper()} ---")

        if isinstance(event, TaskStatusUpdateEvent):
            state = event.status.state.value
            final = " [FINAL]" if event.final else ""
            print(f"  [status] {state}{final}")
        else:
            for part in event.artifact.parts:
                if isinstance(part, TextPart):
                    text = part.text
                    if len(text) > 100:
                        text = text[:100] + "..."
                    append = " (append)" if event.append else ""
                    print(f"  [artifact{append}] {text}")

    print(f"\n{'#' * 72}\n")


async def run_tool_adapter(ticket: str, verbose: bool = False) -> None:
    """Run the pipeline using tool adapters."""
    triage_app = create_triage_app()
    investigator_app = create_investigator_app()
    resolution_app = create_resolution_app()

    pipeline = A2APipeline(
        triage_transport=ASGITransport(app=triage_app),
        investigator_transport=ASGITransport(app=investigator_app),
        resolution_transport=ASGITransport(app=resolution_app),
    )

    result = await pipeline.run_tool_adapter(ticket)
    _print_blocking_result(result)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the A2A multi-agent customer support demo",
    )
    parser.add_argument(
        "--ticket",
        "-t",
        help="Ticket text (reads from stdin if not provided)",
    )
    parser.add_argument(
        "--sample",
        "-s",
        choices=list(SAMPLE_TICKETS.keys()),
        help="Use a pre-built sample ticket",
    )
    parser.add_argument(
        "--mode",
        "-m",
        choices=["blocking", "streaming", "tool-adapter"],
        default="blocking",
        help="Execution mode (default: blocking)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of formatted output",
    )

    args = parser.parse_args()

    # Resolve ticket text
    if args.sample:
        ticket = SAMPLE_TICKETS[args.sample]
    elif args.ticket:
        ticket = args.ticket
    elif not sys.stdin.isatty():
        ticket = sys.stdin.read().strip()
    else:
        parser.error("Provide --ticket, --sample, or pipe text via stdin")

    # Configure logging
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")

    print(f"\n{'#' * 72}")
    print("  OBSCURA A2A MULTI-AGENT DEMO")
    print(f"{'#' * 72}")
    print(f"\nTicket: {ticket}")
    print(f"Mode:   {args.mode}\n")

    # Dispatch
    runners = {
        "blocking": run_blocking,
        "streaming": run_streaming,
        "tool-adapter": run_tool_adapter,
    }
    runner = runners[args.mode]
    asyncio.run(runner(ticket, verbose=args.verbose))


if __name__ == "__main__":
    main()
