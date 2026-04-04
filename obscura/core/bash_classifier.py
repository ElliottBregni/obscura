"""Speculative bash command safety classifier.

Pre-analyzes shell commands for dangerous patterns in a background thread
so the classification is ready before the permission dialog is shown.
This makes the interactive confirmation flow faster — the UI doesn't block
while the classifier runs.

Usage::

    classifier = BashClassifier()
    task = classifier.classify_async("rm -rf /")
    # ... later, when the confirmation dialog fires ...
    result = await task  # Classification(level=BashRisk.DANGEROUS, ...)
"""

from __future__ import annotations

import asyncio
import enum
import re
import time
from dataclasses import dataclass, field


class BashRisk(enum.Enum):
    """Risk level for a shell command."""

    SAFE = "safe"
    NEEDS_REVIEW = "needs-review"
    DANGEROUS = "dangerous"


@dataclass(frozen=True)
class Classification:
    """Result of a bash command safety analysis."""

    level: BashRisk
    reasons: tuple[str, ...] = field(default_factory=tuple)
    latency_ms: int = 0


# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/\b"), "rm with root path"),
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f"), "recursive force delete"),
    (re.compile(r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*r"), "recursive force delete"),
    (re.compile(r"\bmkfs\b"), "filesystem format"),
    (re.compile(r"\bdd\s+if="), "raw disk write"),
    (re.compile(r">\s*/dev/(sd|hd|nvme|disk)"), "redirect to block device"),
    (re.compile(r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;"), "fork bomb"),
    (re.compile(r"\bchmod\s+777\s+/"), "world-writable root path"),
    (re.compile(r"\bcurl\b.*\|\s*(ba)?sh\b"), "pipe download to shell"),
    (re.compile(r"\bwget\b.*\|\s*(ba)?sh\b"), "pipe download to shell"),
    (re.compile(r"\bcurl\b.*\|\s*zsh\b"), "pipe download to shell"),
    (re.compile(r"\bwget\b.*\|\s*zsh\b"), "pipe download to shell"),
    (re.compile(r">\s*/etc/"), "overwrite system config"),
    (re.compile(r"\bgit\s+push\s+.*--force\b"), "force push"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "hard reset"),
]

_REVIEW_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bsudo\b"), "elevated privileges"),
    (re.compile(r"\bchmod\b"), "permission change"),
    (re.compile(r"\bchown\b"), "ownership change"),
    (re.compile(r"\bkill\s+-9\b"), "force kill"),
    (re.compile(r"\bkillall\b"), "kill all processes"),
    (re.compile(r"\bpkill\b"), "pattern kill"),
    (re.compile(r"\brm\s+-[a-zA-Z]*r"), "recursive delete"),
    (re.compile(r"\bgit\s+push\b"), "push to remote"),
    (re.compile(r"\bgit\s+checkout\s+--\s"), "discard changes"),
    (re.compile(r"\bgit\s+clean\b"), "clean untracked files"),
    (re.compile(r"\bgit\s+branch\s+-D\b"), "force delete branch"),
    (re.compile(r"\bnpm\s+publish\b"), "package publish"),
    (re.compile(r"\bpip\s+install\b(?!.*-r\b)(?!.*requirements)"), "pip install"),
    (re.compile(r"\bdocker\s+rm\b"), "remove container"),
    (re.compile(r"\bdocker\s+system\s+prune\b"), "docker prune"),
    (re.compile(r"\|\s*(ba)?sh\b"), "pipe to shell"),
    (re.compile(r"\|\s*zsh\b"), "pipe to shell"),
    (re.compile(r"\beval\b"), "eval execution"),
]


class BashClassifier:
    """Pre-analyzes bash commands for dangerous patterns.

    The classifier is stateless and thread-safe.  Call :meth:`classify`
    synchronously or :meth:`classify_async` to run the analysis in a
    background thread so it's ready by the time the permission dialog fires.
    """

    def classify(self, command: str) -> Classification:
        """Classify a command synchronously."""
        start = time.monotonic()

        if not command or not command.strip():
            return Classification(
                level=BashRisk.SAFE,
                latency_ms=0,
            )

        dangerous_reasons: list[str] = []
        review_reasons: list[str] = []

        for pattern, reason in _DANGEROUS_PATTERNS:
            if pattern.search(command):
                dangerous_reasons.append(reason)

        if dangerous_reasons:
            return Classification(
                level=BashRisk.DANGEROUS,
                reasons=tuple(dangerous_reasons),
                latency_ms=int((time.monotonic() - start) * 1000),
            )

        for pattern, reason in _REVIEW_PATTERNS:
            if pattern.search(command):
                review_reasons.append(reason)

        if review_reasons:
            return Classification(
                level=BashRisk.NEEDS_REVIEW,
                reasons=tuple(review_reasons),
                latency_ms=int((time.monotonic() - start) * 1000),
            )

        return Classification(
            level=BashRisk.SAFE,
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    def classify_async(self, command: str) -> asyncio.Task[Classification]:
        """Start classification in a background thread, return a Task."""
        loop = asyncio.get_running_loop()

        async def _run() -> Classification:
            return await loop.run_in_executor(None, self.classify, command)

        return asyncio.create_task(_run())


__all__ = [
    "BashClassifier",
    "BashRisk",
    "Classification",
]
