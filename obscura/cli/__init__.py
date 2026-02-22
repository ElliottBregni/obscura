"""obscura.cli — re-exports from the unified CLI (obscura.cli.chat_cli).

This module exists for backward compatibility. All real implementation
lives in obscura.cli.chat_cli.
"""

from __future__ import annotations

import argparse
from typing import Any

# Re-export the canonical CLI entry point and click group
from obscura.cli.chat_cli import main, cli  # noqa: F401

# Re-export observe helpers used by tests
from obscura.cli.chat_cli import (  # noqa: F401
    ObservedAgentState,
    collect_observed_agent_states,
    find_stale_agent_ids,
    _render_state_line,
    _parse_iso_datetime,
    _init_cli_telemetry,
    _get_cli_logger,
    _StderrLogger,
    _summarize_tool_input,
)
from obscura.cli.chat_cli import run_observe as _run_observe_impl


def run_observe(args: argparse.Namespace) -> int:
    """Backward-compatible wrapper: accept argparse Namespace."""
    return _run_observe_impl(
        user_id=str(args.user_id),
        email=str(getattr(args, "email", "observe@obscura.local")),
        org_id=str(getattr(args, "org_id", "org-observe")),
        namespace=str(getattr(args, "namespace", "agent:runtime")),
        interval_seconds=float(getattr(args, "interval_seconds", 1.0)),
        stale_seconds=float(getattr(args, "stale_seconds", 20.0)),
        duration_seconds=float(getattr(args, "duration_seconds", 0.0)),
        once=bool(getattr(args, "once", False)),
    )


def build_parser() -> argparse.ArgumentParser:
    """Backward-compatible argparse parser for legacy test imports.

    Returns a minimal parser that can parse the ``observe`` subcommand
    (the only argparse-specific command that tests still exercise).
    """
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


__all__ = [
    "main",
    "cli",
    "build_parser",
    "run_observe",
    "ObservedAgentState",
    "collect_observed_agent_states",
    "find_stale_agent_ids",
    "_render_state_line",
    "_parse_iso_datetime",
    "_init_cli_telemetry",
    "_get_cli_logger",
    "_StderrLogger",
    "_summarize_tool_input",
]
