"""Preflight tool dispatch — ground the model before it answers.

The "answer-first / look-later" failure mode (gpt-5.3-codex confidently
claiming `glab` isn't available, refusing to fetch GitLab MR URLs, etc.)
happens because the model writes its answer from training-data priors
before any tool result is in context. Per-task dedup guards (see
:mod:`obscura.core.stream_guards`) catch *repeated* identical calls but
do nothing about the first fabricated answer.

This module attacks that root cause. When the user's first message
matches a known pattern (``"can you use <cmd>"``, a GitHub/GitLab URL,
etc.), we run a deterministic side-effect-free tool BEFORE the model is
invoked, and inject the result as a synthetic tool-call/tool-result
pair. The model then sees ground truth in context and has no reason to
fabricate.

Distinct from :mod:`obscura.core.preflight` (which validates an agent's
declared environment manifest at startup) — this module operates on the
*user prompt* at run-time, dispatching tools to ground the model.

Design constraints:
* **Side-effect-free tools only.** Preflight runs without explicit user
  permission, so it must be safe — read-only commands (``which``,
  ``--version``, ``gh pr view``).
* **Cheap pattern matching.** Plain regex over the user prompt, no LLM
  classifier. False positives cost a wasted tool call; false negatives
  fall through to normal model behavior.
* **Compose, don't replace.** The user prompt is unchanged. Preflight
  prepends a synthetic ``tool_use`` + ``tool_result`` pair so the
  agent loop sees the user's original ask plus the grounded data.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from re import Match

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreflightMatch:
    """A preflight invocation: the tool name, args, and reason for matching."""

    tool_name: str
    tool_input: dict[str, object]
    reason: str  # human-readable, surfaced in the synthetic tool result


@dataclass(frozen=True)
class PreflightRule:
    """A single preflight rule.

    ``pattern`` is matched (case-insensitive) against the user prompt with
    :func:`re.search`. If it matches, ``build`` is called with the
    :class:`re.Match` and must return a :class:`PreflightMatch` (or None
    to skip). ``build`` is sync — preflight only awaits at the actual
    tool dispatch boundary.
    """

    name: str
    pattern: re.Pattern[str]
    build: Callable[[Match[str]], PreflightMatch | None]


# ---------------------------------------------------------------------------
# Built-in rules
# ---------------------------------------------------------------------------

# "can you use foo" / "is foo installed" / "do you have foo" → which foo.
# Allows an optional article ("the", "a", "an") so "can you use the glab cli"
# captures `glab`, not `the`.
_BINARY_QUESTION_RE = re.compile(
    r"\b(?:can you (?:use|run)|is|do you have|got)"
    r"\s+(?:the\s+|a\s+|an\s+)?"
    r"`?([\w][\w\-]*)`?"
    r"(?:\s+(?:cli|command|installed|available))?\s*\??",
    re.IGNORECASE,
)

# Tokens that look like commands but are English. Avoids preflighting
# on "can you use it", "is the file there", etc.
_BINARY_QUESTION_STOPWORDS: frozenset[str] = frozenset(
    {
        "it", "that", "this", "the", "a", "an", "any", "some", "one",
        "to", "for", "on", "at", "in", "of", "with", "by", "from",
        "i", "me", "you", "we", "they", "he", "she",
        "yes", "no", "ok", "okay", "sure",
        "code", "tool", "tools", "files", "data", "stuff", "things",
        "been", "have", "had", "is", "are", "was", "were", "do", "does",
        "did", "can", "could", "should", "would", "will",
    },
)


def _build_binary_check(m: Match[str]) -> PreflightMatch | None:
    cmd = m.group(1).lower()
    if cmd in _BINARY_QUESTION_STOPWORDS or len(cmd) < 2:
        return None
    return PreflightMatch(
        tool_name="which_command",
        tool_input={"command": cmd},
        reason=(
            f"User asked about a CLI binary (`{cmd}`). Preflight ran "
            f"`which {cmd}` so the answer is grounded, not guessed."
        ),
    )


# GitHub PR URL → gh pr view
_GITHUB_PR_RE = re.compile(
    r"https?://github\.com/([\w\-.]+)/([\w\-.]+)/pull/(\d+)",
    re.IGNORECASE,
)


def _build_github_pr_view(m: Match[str]) -> PreflightMatch | None:
    owner, repo, pr = m.group(1), m.group(2), m.group(3)
    return PreflightMatch(
        tool_name="run_shell",
        tool_input={
            "command": (
                f"gh pr view {pr} -R {owner}/{repo} "
                "--json title,body,state,files,additions,deletions"
            ),
        },
        reason=(
            f"User referenced GitHub PR {owner}/{repo}#{pr}. Preflight "
            "fetched PR metadata via `gh pr view` so review can start "
            "from real data."
        ),
    )


# GitHub issue URL → gh issue view
_GITHUB_ISSUE_RE = re.compile(
    r"https?://github\.com/([\w\-.]+)/([\w\-.]+)/issues/(\d+)",
    re.IGNORECASE,
)


def _build_github_issue_view(m: Match[str]) -> PreflightMatch | None:
    owner, repo, issue = m.group(1), m.group(2), m.group(3)
    return PreflightMatch(
        tool_name="run_shell",
        tool_input={
            "command": (
                f"gh issue view {issue} -R {owner}/{repo} "
                "--json title,body,state,labels"
            ),
        },
        reason=(
            f"User referenced GitHub issue {owner}/{repo}#{issue}. "
            "Preflight fetched issue metadata via `gh issue view`."
        ),
    )


# GitLab MR URL (gitlab.com OR self-hosted) → glab mr view.
# The ``-R`` flag accepts ``host:path`` for non-gitlab.com instances,
# so we capture the hostname separately and pass it through when it
# isn't the public host.
_GITLAB_MR_RE = re.compile(
    r"https?://([\w\-.]+)/([\w\-./]+?)/-/merge_requests/(\d+)",
    re.IGNORECASE,
)


def _build_gitlab_mr_view(m: Match[str]) -> PreflightMatch | None:
    host, repo, mr = m.group(1), m.group(2), m.group(3)
    # Skip non-GitLab hosts that happen to expose ``/-/merge_requests/``
    # in their URL space (rare but the suffix isn't reserved). Only fire
    # when the hostname looks like GitLab.
    host_lower = host.lower()
    if "gitlab" not in host_lower:
        return None
    repo_arg = repo if host_lower == "gitlab.com" else f"{host}:{repo}"
    return PreflightMatch(
        tool_name="run_shell",
        tool_input={
            "command": f"glab mr view {mr} -R {repo_arg}",
        },
        reason=(
            f"User referenced GitLab MR {repo}!{mr} on {host}. Preflight "
            "fetched MR metadata via `glab mr view`."
        ),
    )


# GitLab issue URL → glab issue view
_GITLAB_ISSUE_RE = re.compile(
    r"https?://([\w\-.]+)/([\w\-./]+?)/-/issues/(\d+)",
    re.IGNORECASE,
)


def _build_gitlab_issue_view(m: Match[str]) -> PreflightMatch | None:
    host, repo, issue = m.group(1), m.group(2), m.group(3)
    host_lower = host.lower()
    if "gitlab" not in host_lower:
        return None
    repo_arg = repo if host_lower == "gitlab.com" else f"{host}:{repo}"
    return PreflightMatch(
        tool_name="run_shell",
        tool_input={
            "command": f"glab issue view {issue} -R {repo_arg}",
        },
        reason=(
            f"User referenced GitLab issue {repo}#{issue} on {host}. "
            "Preflight fetched issue metadata via `glab issue view`."
        ),
    )


DEFAULT_RULES: tuple[PreflightRule, ...] = (
    PreflightRule(
        name="binary_question",
        pattern=_BINARY_QUESTION_RE,
        build=_build_binary_check,
    ),
    PreflightRule(
        name="github_pr_url",
        pattern=_GITHUB_PR_RE,
        build=_build_github_pr_view,
    ),
    PreflightRule(
        name="github_issue_url",
        pattern=_GITHUB_ISSUE_RE,
        build=_build_github_issue_view,
    ),
    PreflightRule(
        name="gitlab_mr_url",
        pattern=_GITLAB_MR_RE,
        build=_build_gitlab_mr_view,
    ),
    PreflightRule(
        name="gitlab_issue_url",
        pattern=_GITLAB_ISSUE_RE,
        build=_build_gitlab_issue_view,
    ),
)


# ---------------------------------------------------------------------------
# Match + dispatch
# ---------------------------------------------------------------------------


def find_matches(
    prompt: str,
    rules: tuple[PreflightRule, ...] = DEFAULT_RULES,
) -> list[PreflightMatch]:
    """Find all preflight matches in ``prompt``.

    Each rule fires at most once per prompt. Duplicates (same tool +
    args from different rules) are deduped.
    """
    matches: list[PreflightMatch] = []
    seen: set[tuple[str, str]] = set()
    for rule in rules:
        m = rule.pattern.search(prompt)
        if m is None:
            continue
        result = rule.build(m)
        if result is None:
            continue
        key = (result.tool_name, repr(sorted(result.tool_input.items())))
        if key in seen:
            continue
        seen.add(key)
        matches.append(result)
    return matches


async def run_preflight(
    prompt: str,
    *,
    invoke_tool: Callable[[str, dict[str, object]], Awaitable[str]],
    rules: tuple[PreflightRule, ...] = DEFAULT_RULES,
) -> list[tuple[PreflightMatch, str]]:
    """Match preflight rules against ``prompt`` and dispatch each one.

    ``invoke_tool(name, input)`` is supplied by the caller (the agent
    loop has the registry; this module stays pure). Returns a list of
    ``(match, tool_result_text)`` pairs in the order the matches fired.

    Best-effort: if a tool dispatch raises, we log and skip it — preflight
    failure must not block the user's actual request.
    """
    matches = find_matches(prompt, rules=rules)
    if not matches:
        return []

    results: list[tuple[PreflightMatch, str]] = []
    for match in matches:
        try:
            result_text = await invoke_tool(match.tool_name, dict(match.tool_input))
        except Exception:
            logger.debug(
                "preflight: tool %s failed for prompt match",
                match.tool_name,
                exc_info=True,
            )
            continue
        results.append((match, result_text))
    return results
