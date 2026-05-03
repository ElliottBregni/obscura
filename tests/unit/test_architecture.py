"""Structural guards for the obscura package architecture.

Two complementary checks:

1. ``test_every_obscura_module_imports_cleanly`` — walk-imports every
   submodule of ``obscura`` and asserts none fail. Catches partial-init
   cycles (where a module's eager ``from`` import sees a name that hasn't
   been bound yet) which static analysis can miss.

2. ``test_no_lazy_circular_dep_comments`` — greps the package for
   ``# lazy: avoid circular dep`` comments. The whole module-architecture
   refactor was about removing these; a new one means we re-introduced a
   cycle that should be fixed at the structural level instead of papered
   over with an inline import.

Both run fast (under a second) and are part of the unit suite.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[2] / "obscura"


# ---------------------------------------------------------------------------
# 1. Walk-import every module
# ---------------------------------------------------------------------------


# ``__main__`` modules execute their CLI entrypoint on import, which fails
# under pytest because there is no real TTY. They are not part of the
# import graph we care about.
_SKIP_SUFFIXES = (".__main__",)


def _iter_obscura_modules() -> list[str]:
    """Return all obscura.* submodule names, excluding __main__ entrypoints."""
    import obscura

    return [
        info.name
        for info in pkgutil.walk_packages(obscura.__path__, prefix="obscura.")
        if not any(info.name.endswith(s) for s in _SKIP_SUFFIXES)
    ]


def test_every_obscura_module_imports_cleanly() -> None:
    """Every obscura.* module must import without errors.

    Failures here usually mean someone added a top-level
    ``from obscura.foo import bar`` that creates a partial-init cycle
    with another module. Fix at the structural level (move the shared
    name to a leaf module, or thin one of the package ``__init__.py``
    files via lazy ``__getattr__``); do NOT paper over with an inline
    ``# lazy:`` import inside a function body.
    """
    failures: list[str] = []
    for name in _iter_obscura_modules():
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001  # broad on purpose — surface anything
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    assert not failures, (
        "Modules failed to import (likely a circular dep):\n  - "
        + "\n  - ".join(failures)
    )


# ---------------------------------------------------------------------------
# 2. No ``# lazy: avoid circular dep`` comments
# ---------------------------------------------------------------------------


# Pattern matches the canonical comment shape used historically:
#   # lazy: avoid circular dep ...
#   # lazy: ... circular import ...
#   # lazy: ... cycle ...
#
# Other lazy patterns (e.g. "lazy import to avoid requiring asyncpg unless
# used") are legitimate optional-dependency deferrals, not cycle workarounds.
_CYCLE_LAZY_RE = re.compile(
    r"#\s*lazy:.*\b(?:circular|cycle|partial[-\s]init)\b",
    re.IGNORECASE,
)

# Files we deliberately allow to keep cycle-related lazy comments, with a
# short justification. Empty by default — the whole point of the refactor is
# that none should exist.
_ALLOWED_FILES: dict[str, str] = {}


def _scan_for_cycle_lazy_comments() -> list[str]:
    """Return file:line:line-text matches for lazy-cycle comments."""
    hits: list[str] = []
    for py_file in PKG_ROOT.rglob("*.py"):
        rel = py_file.relative_to(PKG_ROOT.parent).as_posix()
        if rel in _ALLOWED_FILES:
            continue
        try:
            text = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _CYCLE_LAZY_RE.search(line):
                hits.append(f"{rel}:{lineno}: {line.strip()}")
    return hits


def test_no_lazy_circular_dep_comments() -> None:
    """No ``# lazy: avoid circular dep`` (or similar) comments allowed.

    These comments mark inline function-body imports that exist only to
    work around a circular dependency. The fix is structural — move the
    shared symbol to a leaf module, thin a package ``__init__.py`` via
    lazy ``__getattr__``, or extract a sibling decoupling layer.

    See the ``# lazy:`` comment removal commits / ``CLAUDE.md`` for the
    patterns that replaced them.
    """
    hits = _scan_for_cycle_lazy_comments()
    assert not hits, (
        "Found new cycle-workaround lazy imports — fix structurally, "
        "don't paper over with inline imports:\n  - " + "\n  - ".join(hits)
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
