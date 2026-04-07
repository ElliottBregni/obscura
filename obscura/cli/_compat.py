"""Small compatibility layer exposing a few bootstrap helpers lazily.

This file mirrors the earlier session changes that provided minimal
proxies for functions tests import from obscura.cli. Keeping them here
prevents import-time failures for tests that still import these names.
"""

from __future__ import annotations

from typing import Any


def _import_bootstrap():
    from obscura.cli import bootstrap as _b

    return _b


def _discover_mcp(*a, **k):
    return _import_bootstrap()._discover_mcp(*a, **k)


def _parse_inline_agent_mention(*a, **k):
    return _import_bootstrap()._parse_inline_agent_mention(*a, **k)


def _run_inline_agent_from_mention(*a, **k):
    return _import_bootstrap()._run_inline_agent_from_mention(*a, **k)


def _cli_confirm(*a, **k):
    # Forward to the top-level _cli_confirm to avoid importing bootstrap at module import time.
    from obscura.cli import _cli_confirm as _c

    return _c(*a, **k)
