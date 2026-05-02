"""obscura.arbiter.test_runner — Lightweight async test runner for Arbiter gating.

Maps changed source files to their test files via naming conventions,
runs ``pytest -x --tb=line -q`` on them, and caches results by
content hash to avoid re-running tests when files haven't changed.

Usage::

    outcome = await run_related_tests(["obscura/core/task_queue.py"])
    if outcome.failed > 0:
        print(f"Failures: {outcome.failed_tests}")
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

# Module-level cache: content_hash → TestOutcome
_cache: dict[str, "TestOutcome"] = {}


@dataclass(frozen=True)
class TestOutcome:
    """Result of running pytest on a set of test files."""

    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    failed_tests: tuple[str, ...] = field(default_factory=tuple)
    duration_ms: int = 0
    cached: bool = False
    timeout_exceeded: bool = False


def _find_related_tests(
    changed_files: Sequence[str],
    *,
    project_root: str | None = None,
) -> list[str]:
    """Map changed source files to test files via naming conventions.

    Looks for:
    - ``tests/**/test_<name>.py`` (standard pytest layout)
    - ``tests/**/test_<name>_*.py`` (test file with suffix)
    - ``<dir>/test_<name>.py`` (co-located tests)

    Only returns test files that actually exist on disk.
    """
    root = Path(project_root) if project_root else Path.cwd()
    test_files: list[str] = []
    seen: set[str] = set()

    for fpath in changed_files:
        p = Path(fpath)
        if not p.suffix == ".py":
            continue
        # Skip test files themselves.
        if p.name.startswith("test_"):
            if p.is_file():
                resolved = str(p.resolve())
                if resolved not in seen:
                    test_files.append(str(p))
                    seen.add(resolved)
            continue

        stem = p.stem  # e.g. "task_queue"

        # Strategy 1: tests/**/test_<stem>.py
        candidates = list(root.glob(f"tests/**/test_{stem}.py"))

        # Strategy 2: co-located test_<stem>.py
        co_located = p.parent / f"test_{stem}.py"
        if co_located.is_file():
            candidates.append(co_located)

        for c in candidates:
            resolved = str(c.resolve())
            if resolved not in seen:
                test_files.append(str(c))
                seen.add(resolved)

    return test_files


def _content_hash(files: Sequence[str]) -> str:
    """SHA256 of concatenated file contents for cache keying."""
    h = hashlib.sha256()
    for f in sorted(files):
        try:
            h.update(Path(f).read_bytes())
        except Exception:
            h.update(f.encode())
    return h.hexdigest()[:16]


async def run_related_tests(
    changed_files: Sequence[str],
    *,
    timeout_s: float = 10.0,
    project_root: str | None = None,
) -> TestOutcome:
    """Find and run tests related to changed files.

    Uses ``pytest -x --tb=line -q`` (fail-fast, minimal output).
    Results are cached by content hash of source + test files.

    Returns a :class:`TestOutcome`.
    """
    test_files = _find_related_tests(changed_files, project_root=project_root)
    if not test_files:
        return TestOutcome()  # No related tests found — pass by default.

    # Check cache.
    all_files = list(changed_files) + test_files
    cache_key = _content_hash(all_files)
    cached = _cache.get(cache_key)
    if cached is not None:
        from dataclasses import replace

        return replace(cached, cached=True)

    # Run pytest.
    outcome = await _run_pytest(test_files, timeout_s=timeout_s)

    # Cache the result.
    _cache[cache_key] = outcome
    # Evict old cache entries (keep last 50).
    while len(_cache) > 50:
        _cache.pop(next(iter(_cache)))

    return outcome


async def _run_pytest(
    test_files: list[str],
    *,
    timeout_s: float = 10.0,
) -> TestOutcome:
    """Execute pytest in a subprocess and parse the result."""
    cmd = [
        "python",
        "-m",
        "pytest",
        "-x",  # Stop at first failure.
        "--tb=line",  # Minimal traceback.
        "-q",  # Quiet output.
        "--no-header",
        *test_files,
    ]

    start = time.monotonic()
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(Path.cwd()),
            ),
            timeout=timeout_s,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_s,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        stdout = stdout_bytes.decode(errors="replace")
        return _parse_pytest_output(stdout, proc.returncode or 0, duration_ms)
    except asyncio.TimeoutError:
        duration_ms = int((time.monotonic() - start) * 1000)
        return TestOutcome(timeout_exceeded=True, duration_ms=duration_ms)
    except FileNotFoundError:
        logger.debug("pytest not found in PATH")
        return TestOutcome()
    except Exception:
        logger.debug("Test runner failed", exc_info=True)
        return TestOutcome()


def _parse_pytest_output(
    stdout: str,
    exit_code: int,
    duration_ms: int,
) -> TestOutcome:
    """Parse pytest quiet output into a TestOutcome."""
    passed = 0
    failed = 0
    errors = 0
    skipped = 0
    failed_tests: list[str] = []

    for line in stdout.splitlines():
        line = line.strip()
        # Summary line: "5 passed, 2 failed, 1 error in 0.5s"
        if "passed" in line or "failed" in line or "error" in line:
            import re

            for match in re.finditer(r"(\d+)\s+(passed|failed|error|skipped)", line):
                count = int(match.group(1))
                kind = match.group(2)
                if kind == "passed":
                    passed = count
                elif kind == "failed":
                    failed = count
                elif kind == "error":
                    errors = count
                elif kind == "skipped":
                    skipped = count
        # Failed test line: "FAILED tests/test_foo.py::test_bar - AssertionError"
        if line.startswith("FAILED"):
            test_id = line.split(" ", 1)[1].split(" - ")[0] if " " in line else line
            failed_tests.append(test_id.strip())

    # Fallback: use exit code if no summary parsed.
    if exit_code != 0 and failed == 0 and errors == 0:
        errors = 1  # Something went wrong but we couldn't parse it.

    return TestOutcome(
        passed=passed,
        failed=failed,
        errors=errors,
        skipped=skipped,
        failed_tests=tuple(failed_tests),
        duration_ms=duration_ms,
    )
