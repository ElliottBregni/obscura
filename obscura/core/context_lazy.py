"""
obscura.core.context_lazy — Lazy-loading extensions for ContextLoader.

Adds agent-specific skill loading with on-demand body loading to reduce
initial context window bloat.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from obscura.core.frontmatter import parse_frontmatter

logger = logging.getLogger(__name__)


class SkillMetadata:
    """Lightweight skill metadata for lazy loading."""
    
    def __init__(
        self,
        name: str,
        description: str,
        path: Path,
        user_invocable: bool = True,
        allowed_tools: list[str] | None = None,
    ):
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
    
    def __init__(self, skills_dir: Path):
        self.skills_dir = skills_dir
        self._metadata_cache: dict[str, SkillMetadata] = {}
        self._body_cache: dict[str, str] = {}
    
    def discover_skills(self, filter_names: list[str] | None = None) -> list[SkillMetadata]:
        """Discover all skills and load their metadata.
        
        Args:
            filter_names: If provided, only load skills with these names
        
        Returns:
            List of skill metadata objects
        """
        if not self.skills_dir.is_dir():
            return []
        
        skills: list[SkillMetadata] = []
        
        for skill_file in sorted(self.skills_dir.rglob("*.md")):
            if not skill_file.is_file():
                continue
            
            try:
                # Read file and parse frontmatter
                raw = skill_file.read_text(encoding="utf-8").strip()
                if not raw:
                    continue
                
                result = parse_frontmatter(raw, source_path=skill_file)
                meta = result.metadata
                
                name = str(meta.get("name", skill_file.stem))
                
                # Apply name filter if provided
                if filter_names and name not in filter_names:
                    continue
                
                skill_meta = SkillMetadata(
                    name=name,
                    description=str(meta.get("description", "")),
                    path=skill_file,
                    user_invocable=bool(meta.get("user-invocable", meta.get("user_invocable", True))),
                    allowed_tools=meta.get("allowed-tools", meta.get("allowed_tools", [])),
                )
                
                # Cache metadata
                self._metadata_cache[name] = skill_meta
                skills.append(skill_meta)
                
                logger.debug(f"Discovered skill: {name} at {skill_file}")
                
            except Exception as e:
                logger.warning(f"Failed to load skill metadata from {skill_file}: {e}")
                continue
        
        return skills
    
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
            logger.warning(f"Skill '{skill_name}' not found in metadata cache")
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
            logger.error(f"Failed to load skill body for '{skill_name}': {e}")
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
            skills_to_include = [
                s for s in skills_to_include 
                if s.name in skill_names
            ]
        
        if not skills_to_include:
            return ""
        
        stubs = [skill.to_stub() for skill in skills_to_include]
        return "\n\n".join(stubs)
    
    def clear_cache(self) -> None:
        """Clear all caches."""
        self._metadata_cache.clear()
        self._body_cache.clear()
        logger.debug("Skill caches cleared")


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
    ):
        self.name = name
        self.description = description
        self.path = path
        self.argument_hint = argument_hint
        self.allowed_tools = allowed_tools or "*"
        self.denied_tools = denied_tools or []
        self.model = model
        self.builtin_content = builtin_content

    @property
    def tools_enabled(self) -> bool:
        """Whether this command enables any tools (allowed-tools is not empty/none)."""
        return self.allowed_tools != "none"


class ResolvedCommand:
    """A command resolved with arguments substituted into its body."""

    def __init__(self, meta: CommandMetadata, body: str):
        self.meta = meta
        self.name = meta.name
        self.description = meta.description
        self.body = body


class LazyCommandLoader:
    """Discover and load @command markdown files from multiple directories.

    Discovery order: directories are searched in the order provided.
    First match by name wins (earlier directories take precedence).
    """

    def __init__(self, command_dirs: list[Path]):
        self._command_dirs = command_dirs
        self._metadata_cache: dict[str, CommandMetadata] = {}
        self._discovered = False

    def discover_commands(self) -> list[CommandMetadata]:
        """Scan all command directories and built-in defaults, return metadata."""
        if self._discovered:
            return list(self._metadata_cache.values())

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

                    cmd_meta = CommandMetadata(
                        name=name,
                        description=str(meta.get("description", "")),
                        path=cmd_file,
                        argument_hint=str(meta.get("argument-hint", meta.get("argument_hint", ""))),
                        allowed_tools=str(meta.get("allowed-tools", meta.get("allowed_tools", "*"))),
                        denied_tools=_parse_tool_list(meta.get("denied-tools", meta.get("denied_tools"))),
                        model=meta.get("model"),
                    )
                    self._metadata_cache[name] = cmd_meta
                    logger.debug("Discovered command: %s at %s", name, cmd_file)
                except Exception as e:
                    logger.warning("Failed to load command metadata from %s: %s", cmd_file, e)

        # 2. Built-in defaults (only if not already overridden by on-disk)
        try:
            from obscura.core._default_commands import DEFAULT_COMMANDS
        except ImportError:
            DEFAULT_COMMANDS = {}

        for filename, content in DEFAULT_COMMANDS.items():
            name = filename.removesuffix(".md")
            if name in self._metadata_cache:
                continue  # on-disk version takes precedence

            try:
                result = parse_frontmatter(content.strip())
                meta = result.metadata
                cmd_meta = CommandMetadata(
                    name=name,
                    description=str(meta.get("description", "")),
                    argument_hint=str(meta.get("argument-hint", meta.get("argument_hint", ""))),
                    allowed_tools=str(meta.get("allowed-tools", meta.get("allowed_tools", "*"))),
                    denied_tools=_parse_tool_list(meta.get("denied-tools", meta.get("denied_tools"))),
                    model=meta.get("model"),
                    builtin_content=content.strip(),
                )
                self._metadata_cache[name] = cmd_meta
                logger.debug("Loaded built-in command: %s", name)
            except Exception as e:
                logger.warning("Failed to load built-in command '%s': %s", name, e)

        self._discovered = True
        return list(self._metadata_cache.values())

    def resolve_command(self, name: str, arguments: str = "") -> ResolvedCommand | None:
        """Look up a command by name, substitute $ARGUMENTS, and return resolved body."""
        self.discover_commands()

        # Exact match
        cmd = self._metadata_cache.get(name)
        # Case-insensitive fallback
        if cmd is None:
            lowered = name.lower()
            for key, val in self._metadata_cache.items():
                if key.lower() == lowered:
                    cmd = val
                    break
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

            return ResolvedCommand(meta=cmd, body=body)
        except Exception as e:
            logger.error("Failed to resolve command '%s': %s", name, e)
            return None

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
    ):
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
    ):
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
            cases.append(EvalCase(
                name=current_name,
                input_args=current_input,
                criteria=list(current_criteria),
                skills=list(current_skills),
                preferred_tools=list(current_preferred_tools),
            ))

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
    from obscura.core.paths import resolve_all_evals_dirs

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
        pass

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
