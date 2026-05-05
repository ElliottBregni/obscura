"""obscura.core.context_lazy — Lazy-loading extensions for ContextLoader.

Adds agent-specific skill loading with on-demand body loading to reduce
initial context window bloat.
"""

from __future__ import annotations

import contextlib
import difflib
import logging
import re
from typing import TYPE_CHECKING, Any, cast

from obscura.core.frontmatter import parse_frontmatter
from obscura.core.paths import resolve_all_evals_dirs

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Fuzzy-match thresholds shared by command + skill resolution.
# AUTO_PROMOTE: a single candidate at >= this score is silently used (with a
# "did you mean" hint printed by the caller). MARGIN: the runner-up must score
# at least this much lower than the leader for auto-promote to fire — keeps
# ambiguous queries (two equally-close matches) from picking arbitrarily.
# SUGGEST: minimum score to appear in the suggestion list shown on a miss.
_FUZZY_AUTO_PROMOTE = 0.78
_FUZZY_MARGIN = 0.10
_FUZZY_SUGGEST = 0.45


def _fuzzy_score(query: str, candidate: str, description: str = "") -> float:
    """Score how well `candidate` matches `query`.

    Combines: prefix bonus, substring bonus, difflib ratio on name, and a
    weaker difflib ratio on `name + description` so a typo in a name can
    still find help from the description's keywords.
    """
    if not query or not candidate:
        return 0.0
    q = query.lower()
    c = candidate.lower()
    if c == q:
        return 1.0
    score = difflib.SequenceMatcher(None, q, c).ratio()
    if c.startswith(q):
        score = max(score, 0.85 + 0.10 * (len(q) / max(len(c), 1)))
    elif q in c:
        score = max(score, 0.70)
    if description:
        haystack = f"{c} {description.lower()}"
        score = max(
            score,
            0.55 * difflib.SequenceMatcher(None, q, haystack).ratio(),
        )
    return score


def _fuzzy_rank(
    query: str,
    candidates: list[tuple[str, str]],
) -> list[tuple[str, float]]:
    """Rank `candidates` (name, description) by fuzzy score against `query`.

    Returns a list of `(name, score)` sorted high-to-low, filtered to scores
    >= _FUZZY_SUGGEST.
    """
    scored = [(name, _fuzzy_score(query, name, desc)) for name, desc in candidates]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [(n, s) for n, s in scored if s >= _FUZZY_SUGGEST]


class SkillMetadata:
    """Lightweight skill metadata for lazy loading."""

    def __init__(
        self,
        name: str,
        description: str,
        path: Path,
        user_invocable: bool = True,
        allowed_tools: list[str] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.path = path
        self.user_invocable = user_invocable
        self.allowed_tools = allowed_tools or []

    def to_stub(self) -> str:
        """Generate minimal skill stub for system prompt."""
        return f"""---
name: {self.name}
description: {self.description}
---"""


class LazySkillLoader:
    """Lazy loader for agent skills - loads metadata first, body on-demand."""

    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = skills_dir
        self._metadata_cache: dict[str, SkillMetadata] = {}
        self._body_cache: dict[str, str] = {}
        self._file_mtimes: dict[Path, float] = {}

    def discover_skills(
        self,
        filter_names: list[str] | None = None,
    ) -> list[SkillMetadata]:
        """Discover all skills and load their metadata.

        Args:
            filter_names: If provided, only load skills with these names

        Returns:
            List of skill metadata objects

        """
        if not self.skills_dir.is_dir():
            self._metadata_cache.clear()
            self._file_mtimes.clear()
            return []

        seen_paths: set[Path] = set()
        seen_names: set[str] = set()
        skills: list[SkillMetadata] = []

        for skill_file in sorted(self.skills_dir.rglob("*.md")):
            if not skill_file.is_file():
                continue
            seen_paths.add(skill_file)

            try:
                mtime = skill_file.stat().st_mtime
            except OSError:
                logger.debug("stat failed for %s", skill_file, exc_info=True)
                continue

            cached_mtime = self._file_mtimes.get(skill_file)
            if cached_mtime == mtime:
                # Unchanged — find existing metadata by path
                for cached_name, cached_meta in self._metadata_cache.items():
                    if cached_meta.path == skill_file:
                        if not filter_names or cached_name in filter_names:
                            skills.append(cached_meta)
                        seen_names.add(cached_name)
                        break
                continue

            try:
                raw = skill_file.read_text(encoding="utf-8").strip()
                if not raw:
                    self._file_mtimes[skill_file] = mtime
                    continue

                result = parse_frontmatter(raw, source_path=skill_file)
                meta = result.metadata

                name = str(meta.get("name", skill_file.stem))

                if filter_names and name not in filter_names:
                    self._file_mtimes[skill_file] = mtime
                    continue

                skill_meta = SkillMetadata(
                    name=name,
                    description=str(meta.get("description", "")),
                    path=skill_file,
                    user_invocable=bool(
                        meta.get("user-invocable", meta.get("user_invocable", True)),
                    ),
                    allowed_tools=meta.get(
                        "allowed-tools",
                        meta.get("allowed_tools", []),
                    ),
                )

                self._metadata_cache[name] = skill_meta
                self._file_mtimes[skill_file] = mtime
                self._body_cache.pop(name, None)
                skills.append(skill_meta)
                seen_names.add(name)

                logger.debug(f"Discovered skill: {name} at {skill_file}")

            except Exception as e:
                logger.warning(f"Failed to load skill metadata from {skill_file}: {e}")
                continue

        # Evict skills whose files have disappeared
        stale_paths = set(self._file_mtimes) - seen_paths
        for stale in stale_paths:
            del self._file_mtimes[stale]
        if not filter_names:
            stale_names = [
                n for n, m in self._metadata_cache.items() if m.path not in seen_paths
            ]
            for n in stale_names:
                del self._metadata_cache[n]
                self._body_cache.pop(n, None)

        return skills

    def discovered_skills(self) -> list[SkillMetadata]:
        """Return discovered skill metadata (whatever ``discover_skills`` cached)."""
        return list(self._metadata_cache.values())

    def has_skill(self, skill_name: str) -> bool:
        """Whether ``skill_name`` is in the discovered metadata."""
        return skill_name in self._metadata_cache

    def load_skill_body(self, skill_name: str) -> str | None:
        """Load full skill body on-demand.

        Args:
            skill_name: Name of skill to load

        Returns:
            Full skill content (frontmatter + body), or None if not found

        """
        # Check cache first
        if skill_name in self._body_cache:
            logger.debug(f"Skill '{skill_name}' loaded from cache")
            return self._body_cache[skill_name]

        # Get metadata
        if skill_name not in self._metadata_cache:
            # Quiet by default — callers may iterate multiple loaders looking
            # for a skill, so a miss here is normal, not a warning.
            logger.debug(f"Skill '{skill_name}' not found in metadata cache")
            return None

        skill_meta = self._metadata_cache[skill_name]

        try:
            # Load full file
            full_content = skill_meta.path.read_text(encoding="utf-8").strip()

            # Cache it
            self._body_cache[skill_name] = full_content

            logger.info(f"Loaded skill body for: {skill_name}")
            return full_content

        except Exception as e:
            logger.exception(f"Failed to load skill body for '{skill_name}': {e}")
            return None

    def get_skill_stubs(self, skill_names: list[str] | None = None) -> str:
        """Get minimal skill stubs for system prompt.

        Args:
            skill_names: If provided, only include these skills

        Returns:
            Formatted string of skill stubs

        """
        skills_to_include = self._metadata_cache.values()

        if skill_names:
            skills_to_include = [s for s in skills_to_include if s.name in skill_names]

        if not skills_to_include:
            return ""

        stubs = [skill.to_stub() for skill in skills_to_include]
        return "\n\n".join(stubs)

    def clear_cache(self) -> None:
        """Clear all caches."""
        self._metadata_cache.clear()
        self._body_cache.clear()
        self._file_mtimes.clear()
        logger.debug("Skill caches cleared")

    def fuzzy_resolve_name(self, query: str) -> tuple[str | None, str | None]:
        """Resolve a skill name with fuzzy fallback.

        Returns ``(resolved_name, inferred_from)``:
          * exact / case-insensitive hit → ``(name, None)``
          * clear fuzzy winner → ``(name, query)`` so the caller can hint
          * miss or ambiguous → ``(None, None)``
        """
        self.discover_skills()
        if not query:
            return None, None
        if query in self._metadata_cache:
            return query, None
        lowered = query.lower()
        for name in self._metadata_cache:
            if name.lower() == lowered:
                return name, None
        ranked = _fuzzy_rank(
            query,
            [(m.name, m.description) for m in self._metadata_cache.values()],
        )
        if not ranked:
            return None, None
        top_name, top_score = ranked[0]
        if top_score < _FUZZY_AUTO_PROMOTE:
            return None, None
        if len(ranked) > 1 and (top_score - ranked[1][1]) < _FUZZY_MARGIN:
            return None, None
        return top_name, query

    def suggest_skills(self, query: str, limit: int = 5) -> list[str]:
        """Return up to `limit` skill names ranked by fuzzy similarity to `query`."""
        self.discover_skills()
        ranked = _fuzzy_rank(
            query,
            [(m.name, m.description) for m in self._metadata_cache.values()],
        )
        return [name for name, _ in ranked[:limit]]


# ---------------------------------------------------------------------------
# @command loader
# ---------------------------------------------------------------------------


def _parse_tool_list(raw: str | list[str] | None) -> list[str]:
    """Parse a tool list from frontmatter (string or list)."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    return [t.strip() for t in str(raw).split(",") if t.strip()]


def _parse_eval_section(meta: dict[str, Any]) -> tuple[list[str] | None, int | None]:
    """Extract eval criteria and pass_threshold from frontmatter ``eval:`` section.

    Returns ``(criteria_list, pass_threshold)`` — both ``None`` when absent.
    """
    eval_raw = meta.get("eval")
    if not isinstance(eval_raw, dict):
        return None, None
    eval_section = cast(dict[str, Any], eval_raw)
    raw_criteria = eval_section.get("criteria")
    criteria: list[str] | None = None
    if isinstance(raw_criteria, list):
        criteria = [
            str(c).strip() for c in cast(list[Any], raw_criteria) if str(c).strip()
        ]
    threshold: int | None = None
    raw_threshold = eval_section.get("pass_threshold")
    if raw_threshold is not None:
        with contextlib.suppress(ValueError, TypeError):
            threshold = int(raw_threshold)
    return criteria, threshold


class CommandMetadata:
    """Metadata for a markdown @command file."""

    def __init__(
        self,
        name: str,
        description: str,
        path: Path | None = None,
        argument_hint: str = "",
        allowed_tools: str = "*",
        denied_tools: list[str] | None = None,
        model: str | None = None,
        *,
        builtin_content: str | None = None,
        eval_criteria: list[str] | None = None,
        eval_pass_threshold: int | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.path = path
        self.argument_hint = argument_hint
        self.allowed_tools = allowed_tools or "*"
        self.denied_tools = denied_tools or []
        self.model = model
        self.builtin_content = builtin_content
        self.eval_criteria = eval_criteria
        self.eval_pass_threshold = eval_pass_threshold

    @property
    def tools_enabled(self) -> bool:
        """Whether this command enables any tools (allowed-tools is not empty/none)."""
        return self.allowed_tools != "none"


class ResolvedCommand:
    """A command resolved with arguments substituted into its body."""

    def __init__(
        self,
        meta: CommandMetadata,
        body: str,
        inferred_from: str | None = None,
    ) -> None:
        self.meta = meta
        self.name = meta.name
        self.description = meta.description
        self.body = body
        # Set when the user's typed name didn't exact-match and we fuzzy-matched
        # to this command. Callers should print a "did you mean" hint.
        self.inferred_from = inferred_from


class LazyCommandLoader:
    """Discover and load @command markdown files from multiple directories.

    Discovery order: directories are searched in the order provided.
    First match by name wins (earlier directories take precedence).
    """

    def __init__(self, command_dirs: list[Path]) -> None:
        self._command_dirs = command_dirs
        self._metadata_cache: dict[str, CommandMetadata] = {}
        self._discovered = False
        self._dir_mtimes: dict[Path, float] = {}

    def _dirs_changed(self) -> bool:
        for cmd_dir in self._command_dirs:
            if not cmd_dir.is_dir():
                continue
            try:
                mtime = cmd_dir.stat().st_mtime
            except OSError:
                logger.debug("stat failed for %s", cmd_dir, exc_info=True)
                continue
            if self._dir_mtimes.get(cmd_dir) != mtime:
                return True
        return False

    def invalidate(self) -> None:
        """Force a full rescan on the next discover_commands() call."""
        self._discovered = False
        self._metadata_cache.clear()
        self._dir_mtimes.clear()

    def discover_commands(self) -> list[CommandMetadata]:
        """Scan all command directories and built-in defaults, return metadata."""
        if self._discovered and not self._dirs_changed():
            return list(self._metadata_cache.values())

        self._metadata_cache.clear()
        for cmd_dir in self._command_dirs:
            if cmd_dir.is_dir():
                try:
                    self._dir_mtimes[cmd_dir] = cmd_dir.stat().st_mtime
                except OSError:
                    logger.debug("stat failed for %s", cmd_dir, exc_info=True)

        # 1. On-disk commands (user directories take precedence)
        for cmd_dir in self._command_dirs:
            if not cmd_dir.is_dir():
                continue
            for cmd_file in sorted(cmd_dir.glob("*.md")):
                if not cmd_file.is_file():
                    continue
                name = cmd_file.stem
                if name.upper() == "README" or name in self._metadata_cache:
                    continue

                try:
                    raw = cmd_file.read_text(encoding="utf-8").strip()
                    if not raw:
                        continue
                    result = parse_frontmatter(raw, source_path=cmd_file)
                    meta = result.metadata

                    eval_criteria, eval_threshold = _parse_eval_section(meta)
                    cmd_meta = CommandMetadata(
                        name=name,
                        description=str(meta.get("description", "")),
                        path=cmd_file,
                        argument_hint=str(
                            meta.get("argument-hint", meta.get("argument_hint", "")),
                        ),
                        allowed_tools=str(
                            meta.get("allowed-tools", meta.get("allowed_tools", "*")),
                        ),
                        denied_tools=_parse_tool_list(
                            meta.get("denied-tools", meta.get("denied_tools")),
                        ),
                        model=meta.get("model"),
                        eval_criteria=eval_criteria,
                        eval_pass_threshold=eval_threshold,
                    )
                    self._metadata_cache[name] = cmd_meta
                    logger.debug("Discovered command: %s at %s", name, cmd_file)
                except Exception as e:
                    logger.warning(
                        "Failed to load command metadata from %s: %s",
                        cmd_file,
                        e,
                    )

        # 2. Built-in defaults (only if not already overridden by on-disk)
        default_commands: dict[str, str]
        try:
            from obscura.core._default_commands import DEFAULT_COMMANDS

            default_commands = DEFAULT_COMMANDS
        except ImportError:
            logger.debug("suppressed exception in discover_commands", exc_info=True)
            default_commands = {}

        for filename, content in default_commands.items():
            name = filename.removesuffix(".md")
            if name in self._metadata_cache:
                continue  # on-disk version takes precedence

            try:
                result = parse_frontmatter(content.strip())
                meta = result.metadata
                eval_criteria, eval_threshold = _parse_eval_section(meta)
                cmd_meta = CommandMetadata(
                    name=name,
                    description=str(meta.get("description", "")),
                    argument_hint=str(
                        meta.get("argument-hint", meta.get("argument_hint", "")),
                    ),
                    allowed_tools=str(
                        meta.get("allowed-tools", meta.get("allowed_tools", "*")),
                    ),
                    denied_tools=_parse_tool_list(
                        meta.get("denied-tools", meta.get("denied_tools")),
                    ),
                    model=meta.get("model"),
                    builtin_content=content.strip(),
                    eval_criteria=eval_criteria,
                    eval_pass_threshold=eval_threshold,
                )
                self._metadata_cache[name] = cmd_meta
                logger.debug("Loaded built-in command: %s", name)
            except Exception as e:
                logger.warning("Failed to load built-in command '%s': %s", name, e)

        self._discovered = True
        return list(self._metadata_cache.values())

    def resolve_command(self, name: str, arguments: str = "") -> ResolvedCommand | None:
        """Look up a command by name, substitute $ARGUMENTS, and return resolved body.

        Resolution order:
          1. Exact match
          2. Case-insensitive match
          3. Fuzzy match (auto-promotes a clear winner; the result's
             `inferred_from` field is set so callers can show a hint)
        """
        self.discover_commands()

        cmd = self._metadata_cache.get(name)
        inferred_from: str | None = None
        if cmd is None:
            lowered = name.lower()
            for key, val in self._metadata_cache.items():
                if key.lower() == lowered:
                    cmd = val
                    break
        if cmd is None:
            best = self._fuzzy_best(name)
            if best is not None:
                cmd = self._metadata_cache[best]
                inferred_from = name
        if cmd is None:
            return None

        try:
            if cmd.builtin_content is not None:
                raw = cmd.builtin_content
            elif cmd.path is not None:
                raw = cmd.path.read_text(encoding="utf-8").strip()
            else:
                return None
            result = parse_frontmatter(raw, source_path=cmd.path)
            body = result.body

            # Substitute $ARGUMENTS (whole arg string)
            body = body.replace("$ARGUMENTS", arguments)

            # Substitute positional $1, $2, ... from space-split args
            parts = arguments.split() if arguments else []
            for i, part in enumerate(parts, start=1):
                body = body.replace(f"${i}", part)

            # Clean up any remaining positional placeholders
            body = re.sub(r"\$\d+", "", body)

            return ResolvedCommand(meta=cmd, body=body, inferred_from=inferred_from)
        except Exception as e:
            logger.exception("Failed to resolve command '%s': %s", name, e)
            return None

    def _fuzzy_best(self, query: str) -> str | None:
        """Return the single best fuzzy match for `query`, or None if ambiguous."""
        ranked = _fuzzy_rank(
            query,
            [(m.name, m.description) for m in self._metadata_cache.values()],
        )
        if not ranked:
            return None
        top_name, top_score = ranked[0]
        if top_score < _FUZZY_AUTO_PROMOTE:
            return None
        if len(ranked) > 1 and (top_score - ranked[1][1]) < _FUZZY_MARGIN:
            return None  # ambiguous — let the caller show suggestions
        return top_name

    def suggest_commands(self, query: str, limit: int = 5) -> list[str]:
        """Return up to `limit` command names ranked by fuzzy similarity to `query`.

        Used on the miss path so the REPL can say `did you mean: a, b, c`.
        """
        self.discover_commands()
        ranked = _fuzzy_rank(
            query,
            [(m.name, m.description) for m in self._metadata_cache.values()],
        )
        return [name for name, _ in ranked[:limit]]

    def command_names(self) -> list[str]:
        """Return sorted list of discovered command names (for tab completion)."""
        self.discover_commands()
        return sorted(self._metadata_cache.keys())


# ---------------------------------------------------------------------------
# Eval loader
# ---------------------------------------------------------------------------


class EvalCase:
    """A single test case for evaluating a command or skill."""

    def __init__(
        self,
        name: str,
        input_args: str,
        criteria: list[str],
        skills: list[str] | None = None,
        preferred_tools: list[str] | None = None,
    ) -> None:
        self.name = name
        self.input_args = input_args
        self.criteria = criteria
        self.skills = skills or []
        self.preferred_tools = preferred_tools or []


class EvalSuite:
    """A collection of eval cases for a command."""

    def __init__(
        self,
        command_name: str,
        cases: list[EvalCase],
        runs_per_case: int = 1,
    ) -> None:
        self.command_name = command_name
        self.cases = cases
        self.runs_per_case = runs_per_case


def parse_eval_file(content: str) -> list[EvalCase]:
    """Parse an .eval.md file into eval cases.

    Format::

        ---
        runs: 3
        ---

        ## Test: descriptive name
        input: some arguments here
        skills: python, security
        criteria:
          - First criterion
          - Second criterion

        ## Test: another test
        input: different args
        criteria:
          - Must do X
          - Must not do Y
    """
    result = parse_frontmatter(content.strip())
    body = result.body

    cases: list[EvalCase] = []
    current_name = ""
    current_input = ""
    current_criteria: list[str] = []
    current_skills: list[str] = []
    current_preferred_tools: list[str] = []
    in_criteria = False

    def _flush() -> None:
        if current_name and current_criteria:
            cases.append(
                EvalCase(
                    name=current_name,
                    input_args=current_input,
                    criteria=list(current_criteria),
                    skills=list(current_skills),
                    preferred_tools=list(current_preferred_tools),
                ),
            )

    for line in body.splitlines():
        stripped = line.strip()

        if stripped.startswith("## Test:"):
            _flush()
            current_name = stripped.removeprefix("## Test:").strip()
            current_input = ""
            current_criteria = []
            current_skills = []
            current_preferred_tools = []
            in_criteria = False

        elif stripped.startswith("input:"):
            current_input = stripped.removeprefix("input:").strip()
            in_criteria = False

        elif stripped.startswith("skills:"):
            raw = stripped.removeprefix("skills:").strip()
            current_skills = [s.strip() for s in raw.split(",") if s.strip()]
            in_criteria = False

        elif stripped.startswith("preferred-tools:"):
            raw = stripped.removeprefix("preferred-tools:").strip()
            current_preferred_tools = [t.strip() for t in raw.split(",") if t.strip()]
            in_criteria = False

        elif stripped == "criteria:":
            in_criteria = True

        elif in_criteria and stripped.startswith("- "):
            current_criteria.append(stripped.removeprefix("- ").strip())

        elif in_criteria and stripped.startswith("* "):
            current_criteria.append(stripped.removeprefix("* ").strip())

    _flush()
    return cases


def _try_parse_eval(raw: str, cmd_name: str, source: str = "") -> EvalSuite | None:
    """Parse raw eval content into an EvalSuite, or None on failure."""
    try:
        result = parse_frontmatter(raw.strip())
        runs = int(result.metadata.get("runs", 1))
        cases = parse_eval_file(raw)
        if cases:
            return EvalSuite(cmd_name, cases, runs_per_case=runs)
    except Exception as e:
        logger.warning("Failed to parse eval %s: %s", source, e)
    return None


def load_eval_for_command(cmd: CommandMetadata) -> EvalSuite | None:
    """Load an .eval.md file for a command.

    Search order:
    1. Sibling file: ``review.eval.md`` next to ``review.md``
    2. Evals directory: ``.obscura/evals/review.eval.md``
    3. Built-in eval defaults
    """
    # 1. On-disk sibling
    if cmd.path is not None:
        eval_path = cmd.path.with_suffix(".eval.md")
        if eval_path.is_file():
            raw = eval_path.read_text(encoding="utf-8")
            suite = _try_parse_eval(raw, cmd.name, str(eval_path))
            if suite:
                return suite

    # 2. Evals directories (.obscura/evals/)
    for evals_dir in resolve_all_evals_dirs():
        eval_path = evals_dir / f"{cmd.name}.eval.md"
        if eval_path.is_file():
            raw = eval_path.read_text(encoding="utf-8")
            suite = _try_parse_eval(raw, cmd.name, str(eval_path))
            if suite:
                return suite

    # 3. Built-in eval defaults
    try:
        from obscura.core._default_evals import DEFAULT_EVALS

        if cmd.name in DEFAULT_EVALS:
            return _try_parse_eval(DEFAULT_EVALS[cmd.name], cmd.name, "built-in")
    except ImportError:
        logger.debug("suppressed exception in load_eval_for_command", exc_info=True)

    return None


EVAL_GRADING_PROMPT = """\
You are an eval grader. You will be given:
1. The original command and input
2. The LLM's response
3. A list of criteria to evaluate against

For each criterion, respond with PASS or FAIL and a brief (one sentence) reason.
Then give an overall score as a fraction (e.g., 3/5).

## Command
@{command} {input}

## Response
{response}

## Criteria
{criteria}

## Output format
| # | Criterion | Result | Reason |
|---|-----------|--------|--------|
| 1 | ... | PASS/FAIL | ... |

**Score: X/{total}**
"""
