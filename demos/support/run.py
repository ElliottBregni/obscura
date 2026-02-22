"""
demos.support.run — CLI entry point for the customer support pipeline.

Usage::

    python -m demos.support.run --ticket "I was charged twice (cust_001)"
    echo "My API keeps timing out (cust_003)" | python -m demos.support.run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from demos.support.orchestrator import SupportPipeline
from obscura.demo.framework import make_demo_user


# ---------------------------------------------------------------------------
# Mock user for demo
# ---------------------------------------------------------------------------

DEMO_USER = make_demo_user("agent:copilot")


# ---------------------------------------------------------------------------
# Sample tickets for quick demo
# ---------------------------------------------------------------------------

SAMPLE_TICKETS = {
    "billing": "I was charged twice for my subscription this month. Customer ID: cust_001. Please fix ASAP.",
    "technical": "Our webhook endpoint stopped receiving events yesterday. We've checked our SSL cert and it looks fine. Customer: cust_003",
    "account": "I can't log into the dashboard after resetting my password. Tried clearing cookies already. (cust_002)",
    "general": "Can you send us your SOC2 compliance documentation? We need it for our vendor review. Customer ID: cust_003",
    "urgent": "URGENT: Our entire API integration is down. Production is broken and we're losing revenue. Customer ID: cust_001",
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_section(title: str, content: str) -> None:
    width = 70
    print(f"\n{'=' * width}")
    print(f"  {title}")
    print(f"{'=' * width}")
    print(content)


async def run_pipeline(ticket: str, verbose: bool = False) -> None:
    """Run the full support pipeline and print results."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

    print(f"\n{'#' * 70}")
    print("  OBSCURA CUSTOMER SUPPORT PIPELINE")
    print(f"{'#' * 70}")
    print(f"\nTicket: {ticket}\n")

    pipeline = SupportPipeline(user=DEMO_USER)

    try:
        result = await pipeline.run(ticket)
    except Exception as e:
        print(f"\nPipeline error: {e}", file=sys.stderr)
        raise

    # -- Triage results ----------------------------------------------------
    _print_section("TRIAGE", "\n".join([
        f"  Category:  {result.triage.category}",
        f"  Severity:  {result.triage.severity}",
        f"  Urgency:   {result.triage.urgency_detected}",
        f"  Customer:  {result.triage.customer_id}",
        f"  Routing:   {result.triage.routing}",
        f"  Plan:      {result.triage.customer_info.get('plan', 'unknown') if result.triage.customer_info else 'N/A'}",
    ]))

    # -- Investigation results ---------------------------------------------
    if result.investigation:
        _print_section("INVESTIGATION", "\n".join([
            f"  Root cause:     {result.investigation.root_cause}",
            f"  Similar tix:    {len(result.investigation.similar_tickets)}",
            f"  KB articles:    {len(result.investigation.kb_articles)}",
            f"  Should escalate: {result.investigation.should_escalate}",
            f"  Action:         {result.investigation.recommended_action[:80]}...",
        ]))
    elif result.escalated:
        _print_section("INVESTIGATION", "  Skipped — ticket escalated directly from triage")

    # -- Resolution results ------------------------------------------------
    if result.resolution:
        _print_section("RESOLUTION", "\n".join([
            f"  Type: {result.resolution.response_type}",
            f"  Time: {result.resolution.resolution_time_ms:.1f}ms",
            "",
            "  Customer Message:",
            "  " + result.resolution.customer_message.replace("\n", "\n  "),
        ]))

    # -- Audit trail -------------------------------------------------------
    _print_section("AUDIT TRAIL", "\n".join([
        f"  Phases completed:  {result.phases_completed}",
        f"  Hooks fired:       {len(result.hooks_fired)}",
        f"  Total time:        {result.total_time_ms:.1f}ms",
        f"  Escalated:         {result.escalated}",
        f"  Timestamp:         {result.timestamp}",
    ]))

    if verbose:
        _print_section("HOOKS (detail)", "\n".join(
            f"    {i+1}. {h}" for i, h in enumerate(result.hooks_fired)
        ))

    print(f"\n{'#' * 70}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Obscura customer support demo pipeline",
    )
    parser.add_argument(
        "--ticket", "-t",
        help="Ticket text (reads from stdin if not provided)",
    )
    parser.add_argument(
        "--sample", "-s",
        choices=list(SAMPLE_TICKETS.keys()),
        help="Use a pre-built sample ticket",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging (DEBUG level)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON result instead of formatted output",
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

    if args.json:
        async def _json_run() -> None:
            logging.basicConfig(level=logging.WARNING)
            pipeline = SupportPipeline(user=DEMO_USER)
            result = await pipeline.run(ticket)
            print(json.dumps(result.to_dict(), indent=2))

        asyncio.run(_json_run())
    else:
        asyncio.run(run_pipeline(ticket, verbose=args.verbose))


if __name__ == "__main__":
    main()
