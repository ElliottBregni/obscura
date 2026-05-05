"""obscura.core.context_suggestions — Smart file context recommendations.

Recommends files the agent should read based on recent edits and
import relationships.
"""

from __future__ import annotations

import re
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


def suggest_files(
    recently_modified: list[str],
    recently_read: list[str],
    *,
    max_suggestions: int = 5,
) -> list[dict[str, str]]:
    """Suggest files to read based on recent activity.

    Looks at:
      1. Files that import/reference recently modified files
      2. Test files for recently modified source files
      3. Config files related to modified code

    Returns list of {"path": ..., "reason": ...} suggestions.
    """
    suggestions: list[dict[str, str]] = []
    already_seen = set(recently_read) | set(recently_modified)

    for modified_path in recently_modified:
        p = Path(modified_path)
        if not p.exists():
            continue

        # 1. Suggest test file for modified source.
        test_path = _find_test_file(p)
        if test_path and str(test_path) not in already_seen:
            suggestions.append(
                {
                    "path": str(test_path),
                    "reason": f"Test file for {p.name}",
                },
            )
            already_seen.add(str(test_path))

        # 2. Suggest __init__.py if editing a module file.
        init_path = p.parent / "__init__.py"
        if init_path.exists() and str(init_path) not in already_seen and init_path != p:
            suggestions.append(
                {
                    "path": str(init_path),
                    "reason": f"Package init for {p.parent.name}/",
                },
            )
            already_seen.add(str(init_path))

        # 3. Suggest files that import the modified file.
        importers = _find_importers(p, max_results=2)
        for imp_path in importers:
            if str(imp_path) not in already_seen:
                suggestions.append(
                    {
                        "path": str(imp_path),
                        "reason": f"Imports {p.name}",
                    },
                )
                already_seen.add(str(imp_path))

        if len(suggestions) >= max_suggestions:
            break

    return suggestions[:max_suggestions]


def _find_test_file(source_path: Path) -> Path | None:
    """Find the test file corresponding to a source file."""
    name = source_path.stem
    parent = source_path.parent

    # Common test file patterns.
    candidates = [
        parent / f"test_{name}.py",
        parent / "tests" / f"test_{name}.py",
        parent.parent / "tests" / f"test_{name}.py",
        parent.parent / "tests" / parent.name / f"test_{name}.py",
    ]

    # Also check tests/ at project root.
    for ancestor in source_path.parents:
        tests_dir = ancestor / "tests"
        if tests_dir.is_dir():
            # Search recursively for test_<name>.py
            matches = list(tests_dir.rglob(f"test_{name}.py"))
            if matches:
                return matches[0]
            break

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _find_importers(target: Path, max_results: int = 3) -> list[Path]:
    """Find Python files that import the target module (shallow search)."""
    module_name = target.stem
    results: list[Path] = []

    # Search in same directory + parent.
    search_dirs = [target.parent]
    if target.parent.parent.is_dir():
        search_dirs.append(target.parent.parent)

    pattern = re.compile(
        rf"\b(?:from\s+\S*{re.escape(module_name)}\s+import|import\s+\S*{re.escape(module_name)})\b",
    )

    for search_dir in search_dirs:
        for py_file in search_dir.glob("*.py"):
            if py_file == target or len(results) >= max_results:
                continue
            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
                if pattern.search(content):
                    results.append(py_file)
            except OSError:
                logger.debug("suppressed exception in _find_importers", exc_info=True)
                continue

    return results
