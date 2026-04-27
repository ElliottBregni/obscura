"""obscura.core.output_quality — Detect hallucinated UX claims in model output.

Some models (notably Kimi K2.5, Llama-derived locals) confuse Claude
Code's permission UX with obscura's. They invent prompts that don't
exist:

  * "click Allow on the permission dialog"
  * "press 'a' to allow once"
  * "/allowed-tools mcp__obs__..."
  * "claude --allowedTools=..."

These phrases appear after a tool returns successfully (or even before
it returns), telling the user to take an action that does nothing
because there is no such UI in obscura. The user has to manually
correct the model, often repeatedly.

Prompt rules (default_agent.txt rule 9) help but don't fully suppress
the behaviour — Kimi seems to weight its training data on Claude Code
above the system prompt for these specific patterns. This module
adds a runtime scanner: at TURN_COMPLETE the agent loop runs
``scan_text`` over the accumulated turn text and logs a structured
WARNING when a known hallucination pattern fires. The warning carries
a snippet so dev / observability tooling can surface it.

We deliberately don't *suppress* or *rewrite* the output — that would
hide the issue and make debugging harder. Detect, log, move on. A
future enhancement could feed the violation into a corrective system
message for the next turn, but that needs an explicit feedback loop
the agent loop doesn't currently have.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Violation:
    """One hallucination match in model output."""

    pattern_name: str
    snippet: str
    """The matched text plus a few words of surrounding context."""


@dataclass(frozen=True)
class _Pattern:
    name: str
    regex: re.Pattern[str]


# Pattern set seeded from real hallucinations seen in obscura sessions.
# Each pattern targets a UX element that exists in Claude Code but not in
# obscura — the model confusing the two is the failure mode.
_PATTERNS: tuple[_Pattern, ...] = (
    _Pattern(
        name="claude_code_allow_button",
        regex=re.compile(
            r"\b(click|press|tap|hit)\s+\"?Allow\"?",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        name="claude_code_press_a",
        regex=re.compile(
            r"\bpress\s+`?[aA]`?\s+(to\s+)?(allow|approve)",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        name="allowed_tools_slash",
        regex=re.compile(r"/allowed[-_ ]tools\b", re.IGNORECASE),
    ),
    _Pattern(
        name="allowed_tools_flag",
        regex=re.compile(
            r"--?allowed[-_ ]?tools\b",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        name="policy_allow_slash",
        regex=re.compile(r"/policy\s+allow\b", re.IGNORECASE),
    ),
    _Pattern(
        name="grant_one_time_permission",
        regex=re.compile(
            r"\b(one-time\s+)?permission\s+grant\b",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        name="claude_code_sandbox",
        regex=re.compile(
            r"Claude\s+Code['']?s?\s+(permission|outer|sandbox)",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        # Catches "outer sandbox", "outer permission wall", "outer
        # permission layer" — the model rephrasing "Claude Code sandbox"
        # without the literal Claude Code prefix.
        name="outer_layer",
        regex=re.compile(
            r"\bouter\s+(sandbox|permission|layer|wall)",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        # The model claiming a tool is "still blocked" / "still erroring"
        # right after user_interact returned {"approved": true}. There's
        # no further blocking layer in obscura — if a tool's actual
        # result is success, narrating it as "still blocking" is the
        # hallucination.
        name="still_blocking_after_approval",
        regex=re.compile(
            r"\bstill\s+(blocked?|blocking|erroring|gated|denied)",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        name="despite_approval",
        regex=re.compile(
            r"\bdespite\s+(the\s+)?(in[-\s]?session\s+)?(approval|grant|permission)",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        # "via the Bash tool path instead", "via a different path", etc.
        # When the agent invents alternative routing paths to escape a
        # phantom permission layer.
        name="alt_tool_path",
        regex=re.compile(
            r"\bvia\s+(the\s+)?\w+\s+tool\s+path\b",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        name="approve_in_dialog",
        regex=re.compile(
            r"\b(approve|grant)\s+(\S+\s+){0,3}(dialog|prompt|popup)\b",
            re.IGNORECASE,
        ),
    ),
    _Pattern(
        name="reply_grant_or_yes",
        regex=re.compile(
            r"\breply\s+\*?\*?[\"']?(grant|yes|allow)[\"']?\*?\*?",
            re.IGNORECASE,
        ),
    ),
)


def scan_text(text: str, *, context_chars: int = 40) -> list[Violation]:
    """Find every hallucination pattern that fires in *text*.

    Returns each match once with a ``±context_chars`` window of surrounding
    text so warnings carry useful context. Empty text returns ``[]``.
    """
    if not text:
        return []

    violations: list[Violation] = []
    for pat in _PATTERNS:
        for m in pat.regex.finditer(text):
            start = max(0, m.start() - context_chars)
            end = min(len(text), m.end() + context_chars)
            snippet = text[start:end].strip().replace("\n", " ")
            violations.append(Violation(pattern_name=pat.name, snippet=snippet))
    return violations


def log_violations(violations: list[Violation], *, turn: int = 0) -> None:
    """Emit a structured WARNING per violation.

    Centralised so call sites don't have to format consistently. Logs at
    WARNING so violations surface in default-level logs without flooding.
    """
    for v in violations:
        logger.warning(
            "Hallucinated UX claim (turn=%d, pattern=%s): %s",
            turn,
            v.pattern_name,
            v.snippet,
        )


@dataclass(frozen=True)
class ToolResultSummary:
    """Compact view of a successful tool result, used to build corrections."""

    tool_name: str
    snippet: str
    """First ~200 chars of the tool's stringified result."""


def build_correction_prompt(
    violations: list[Violation],
    successful_tools: list[ToolResultSummary],
) -> str:
    """Build a corrective system message to inject into the next turn.

    Returns an empty string when there's nothing to correct (no
    violations, or no successful tools to point at as ground truth).
    """
    if not violations or not successful_tools:
        return ""

    bullets: list[str] = []
    for s in successful_tools:
        bullets.append(f"- `{s.tool_name}` returned: {s.snippet}")

    pattern_list = ", ".join(sorted({v.pattern_name for v in violations}))

    return (
        "[OBSCURA CORRECTION] Your previous response narrated tool failure "
        "or invoked a phantom permission UI, but the actual tool calls in "
        "that turn succeeded. Patterns flagged: "
        f"{pattern_list}.\n\n"
        "Ground-truth tool results from the same turn:\n"
        + "\n".join(bullets)
        + "\n\nThere is NO outer permission layer in obscura. "
        "`user_interact(mode=permission)` returning `approved: true` IS "
        "the grant. There are no slash commands `/allowed-tools` or "
        "`/policy allow`. Re-read the actual results above and respond "
        "accurately. Do not narrate failure when the tool succeeded."
    )
