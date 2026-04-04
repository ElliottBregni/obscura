"""obscura.tools.system.diff_utils — Structured unified diff generation.

Provides a lightweight ``compute_unified_diff`` helper that returns a
JSON-serialisable dict with hunks, insertions/deletions counts, and a
human-readable summary string.  Designed for use by ``edit_text_file``
and ``write_text_file`` to include structured diffs in their responses.
"""

from __future__ import annotations

import difflib
from typing import Any


def compute_unified_diff(
    old: str,
    new: str,
    path: str,
    *,
    context_lines: int = 3,
) -> dict[str, Any]:
    """Compute a unified diff between *old* and *new* content.

    Returns a JSON-serialisable dict::

        {
            "hunks": [
                {
                    "header": "@@ -1,3 +1,4 @@",
                    "lines": [" ctx", "-old", "+new", " ctx"],
                }
            ],
            "insertions": 2,
            "deletions": 1,
            "summary": "2 insertions, 1 deletion",
            "unified": "<full unified diff text>",
        }
    """
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)

    diff_iter = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=path,
        tofile=path,
        n=context_lines,
    )
    diff_lines = list(diff_iter)

    hunks: list[dict[str, Any]] = []
    current_hunk: dict[str, Any] | None = None
    insertions = 0
    deletions = 0

    for line in diff_lines:
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            current_hunk = {"header": line.rstrip(), "lines": []}
            hunks.append(current_hunk)
            continue
        if current_hunk is None:
            continue

        display = line.rstrip("\n")
        current_hunk["lines"].append(display)
        if line.startswith("+"):
            insertions += 1
        elif line.startswith("-"):
            deletions += 1

    ins_word = "insertion" if insertions == 1 else "insertions"
    del_word = "deletion" if deletions == 1 else "deletions"
    summary = f"{insertions} {ins_word}, {deletions} {del_word}"

    return {
        "hunks": hunks,
        "insertions": insertions,
        "deletions": deletions,
        "summary": summary,
        "unified": "".join(diff_lines),
    }
