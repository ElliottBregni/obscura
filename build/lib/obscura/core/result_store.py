"""Large tool result persistence.

When a tool result exceeds a size threshold (default 200 KB), the full
content is written to disk and a truncated preview is returned to the
model.  This prevents context window blowout while preserving full data
for debugging or user inspection.

Usage::

    store = ResultStore()
    preview, path = store.maybe_persist("call-abc123", giant_text)
    # preview is the first 200KB + footer
    # path is ~/.obscura/tool-results/call-abc123.txt (or None if small)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_BASE_DIR = os.path.expanduser("~/.obscura/tool-results")
_DEFAULT_THRESHOLD = 200_000  # 200 KB


class ResultStore:
    """Persists large tool results to disk and returns truncated previews.

    Parameters
    ----------
    base_dir:
        Directory for persisted results.  Created on first write.
    threshold:
        Results larger than this (in chars) are persisted.

    """

    def __init__(
        self,
        base_dir: str = _DEFAULT_BASE_DIR,
        threshold: int = _DEFAULT_THRESHOLD,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._threshold = threshold

    def maybe_persist(
        self,
        call_id: str,
        text: str,
    ) -> tuple[str, str | None]:
        """Persist *text* if it exceeds the threshold.

        Returns
        -------
        (preview, full_path)
            *preview* is either the original text (if small enough) or a
            truncated version with a footer.  *full_path* is the on-disk
            path if the result was persisted, otherwise ``None``.

        """
        if len(text) <= self._threshold:
            return text, None

        # Write full result to disk
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in call_id)
        path = self._base_dir / f"{safe_id}.txt"

        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        except OSError:
            logger.warning("Failed to persist large result to %s", path, exc_info=True)
            # Fall back to simple truncation
            return (
                text[: self._threshold]
                + f"\n... [truncated, {len(text):,} chars total]",
                None,
            )

        preview = (
            text[: self._threshold] + f"\n... [truncated, {len(text):,} chars total"
            f" — full result at {path}]"
        )
        return preview, str(path)

    @staticmethod
    def read_full(path: str) -> str:
        """Read a previously persisted full result from disk."""
        return Path(path).read_text(encoding="utf-8")


__all__ = ["ResultStore"]
