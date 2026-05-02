"""obscura.cli.repl — backwards-compat re-exports from session.py.

The canonical implementation now lives in :mod:`obscura.cli.session`.
This module is kept for any stale imports.  All symbols are re-exported
lazily to avoid triggering the session → bootstrap → render import chain
before the CLI is ready.
"""

from __future__ import annotations


def __getattr__(name: str):  # noqa: ANN001
    from obscura.cli import session

    return getattr(session, name)
