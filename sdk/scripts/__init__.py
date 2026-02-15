"""Compatibility shim for CLI helpers.

Tests import ``sdk.scripts.cli``; this package re-exports the public
symbols from ``scripts.obscura_cli`` without moving the original file.
"""

from .cli import build_parser, main, _AGENT_COMMANDS, _StderrLogger, _run  # noqa: F401
