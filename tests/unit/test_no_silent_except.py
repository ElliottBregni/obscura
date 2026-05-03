"""Structural guard: no silent except blocks.

Every ``except`` handler in ``obscura/`` must either log (debug/info/warning/
error/exception/critical/log via a logger-like object), re-raise, or be
``contextlib.suppress(...)`` (which we don't see here — it isn't an
``ExceptHandler`` node).

The guard exists because silent ``except: pass`` patterns hide real bugs.
If you intentionally want to swallow an exception, log at debug level so it
shows up in deep logs / `--verbose` runs. If you truly need silent
suppression, use ``contextlib.suppress(...)`` so the intent is obvious.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parents[2] / "obscura"

LOG_METHODS = frozenset(
    {
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "exception",
        "critical",
        "log",
    }
)
LOGGER_LIKE_NAMES = frozenset(
    {"logger", "_logger", "log", "_log", "LOG", "LOGGER", "dlog"}
)


def _handler_logs(handler: ast.ExceptHandler) -> bool:
    for stmt in handler.body:
        for node in ast.walk(stmt):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # logger.debug(...) / self.logger.error(...) / dlog.session_event(...)
            if isinstance(func, ast.Attribute) and func.attr in LOG_METHODS:
                return True
            # logger(...) used as a callable (rare, but accept)
            if isinstance(func, ast.Name) and func.id in LOGGER_LIKE_NAMES:
                return True
            # dlog.<anything>(...) — the deep-log singleton is logger-like.
            if isinstance(func, ast.Attribute):
                base = func.value
                if isinstance(base, ast.Name) and base.id in LOGGER_LIKE_NAMES:
                    return True
    return False


def _handler_reraises(handler: ast.ExceptHandler) -> bool:
    for stmt in handler.body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Raise):
                return True
    return False


def _iter_python_files() -> list[Path]:
    return [p for p in PKG_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def _find_violations() -> list[tuple[Path, int, str]]:
    """Return (file, lineno, exception_type) for each violating handler."""
    violations: list[tuple[Path, int, str]] = []
    for path in _iter_python_files():
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for handler in node.handlers:
                if _handler_logs(handler) or _handler_reraises(handler):
                    continue
                exc_type = (
                    ast.unparse(handler.type) if handler.type else "BaseException"
                )
                violations.append((path, handler.lineno, exc_type))
    return violations


@pytest.mark.unit
def test_no_silent_except_blocks_in_obscura() -> None:
    """Every ``except`` in obscura/ must log or re-raise."""
    violations = _find_violations()
    if not violations:
        return
    rel = PKG_ROOT.parent
    formatted = "\n".join(
        f"  {p.relative_to(rel)}:{lineno}  except {exc_type}:"
        for p, lineno, exc_type in violations
    )
    pytest.fail(
        f"{len(violations)} except handler(s) in obscura/ neither log nor "
        f're-raise. Add `logger.debug("...", exc_info=True)` or use '
        f"`contextlib.suppress(...)` if silence is truly intentional:\n"
        f"{formatted}"
    )
