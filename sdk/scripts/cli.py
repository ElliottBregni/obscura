"""Shim module so tests can import SDK CLI helpers from the sdk package."""

from scripts.obscura_cli import (  # type: ignore
    _AGENT_COMMANDS,
    _StderrLogger,
    _run,
    build_parser,
    main,
)

__all__ = ["build_parser", "main", "_AGENT_COMMANDS", "_StderrLogger", "_run"]
