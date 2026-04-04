r"""obscura.core.compiler.prompt_expansion — Expand $skill and @command references.

Scans instruction text for ``$skillname`` and ``@commandname [args]`` tokens
and replaces them with the resolved skill body or command body.  This runs
at compile time (after merge, before freeze) so that ``agents.yaml`` and
workspace specs can reference skills and commands inline.

Usage::

    from obscura.core.compiler.prompt_expansion import expand_prompt_references

    expanded = expand_prompt_references("$python $security\nYou review code.")
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Match $skill or @command tokens at line start or after whitespace.
# Captures the prefix ($ or @), the name, and any trailing args (for @commands).
_SKILL_RE = re.compile(r"(?:^|(?<=\s))\$([a-zA-Z][a-zA-Z0-9_-]*)")
_COMMAND_RE = re.compile(r"(?:^|(?<=\s))@([a-zA-Z][a-zA-Z0-9_-]*)(?:\s+(.+?))?(?=\n|$)")


def expand_prompt_references(text: str) -> str:
    """Expand ``$skill`` and ``@command`` references in *text*.

    - ``$skillname`` is replaced with the full skill body.
    - ``@commandname args`` is replaced with the resolved command body
      (with ``$ARGUMENTS`` / ``$1`` / ``$2`` substitution).
    - Tokens that don't resolve to a known skill/command are left as-is.
    - Expanded bodies are NOT re-scanned (prevents infinite recursion).

    Returns the expanded text.
    """
    if not text or ("$" not in text and "@" not in text):
        return text

    # Lazy-import to avoid circular imports at module level
    from obscura.core.context_lazy import LazyCommandLoader, LazySkillLoader
    from obscura.core.paths import resolve_all_commands_dirs, resolve_all_skills_dirs

    # Build loaders (cached per call; compile_agent is called infrequently)
    skill_loaders = [LazySkillLoader(d) for d in resolve_all_skills_dirs()]
    command_loader = LazyCommandLoader(resolve_all_commands_dirs())

    def _resolve_skill(name: str) -> str | None:
        for loader in skill_loaders:
            for s in loader.discover_skills():
                if s.name == name or s.name.lower() == name.lower():
                    return loader.load_skill_body(s.name)
        return None

    # Process line-by-line to handle mixed content properly
    lines = text.split("\n")
    expanded_lines: list[str] = []

    for line in lines:
        stripped = line.strip()

        # Check if this line is purely $skill / @command tokens
        # (matching the chained-input pattern: $a $b @cmd args)
        if stripped and (stripped[0] in ("$", "@")):
            result = _expand_chained_line(stripped, _resolve_skill, command_loader)
            if result is not None:
                expanded_lines.append(result)
                continue

        # For mixed lines, only expand $skill inline tokens
        def _replace_skill(m: re.Match[str]) -> str:
            name = m.group(1)
            body = _resolve_skill(name)
            if body is None:
                return m.group(0)  # leave as-is
            logger.debug("Expanded $%s in prompt", name)
            return body

        expanded_lines.append(_SKILL_RE.sub(_replace_skill, line))

    return "\n".join(expanded_lines)


def _expand_chained_line(
    line: str,
    resolve_skill: object,
    command_loader: object,
) -> str | None:
    """Expand a line that starts with $ or @ tokens (chained format).

    Returns expanded text or None if no expansion was possible.
    """
    from obscura.core.context_lazy import LazyCommandLoader

    assert callable(resolve_skill)
    assert isinstance(command_loader, LazyCommandLoader)

    tokens = line.split()
    blocks: list[str] = []
    i = 0

    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("$") and len(tok) > 1:
            name = tok[1:]
            body = resolve_skill(name)
            if body is not None:
                blocks.append(body)
                logger.debug("Expanded $%s in chained prompt", name)
            else:
                # Unknown skill — leave the rest unexpanded
                return None
            i += 1
        elif tok.startswith("@") and len(tok) > 1:
            cmd_name = tok[1:]
            remaining = " ".join(tokens[i + 1 :])
            resolved = command_loader.resolve_command(cmd_name, remaining)
            if resolved is not None:
                blocks.append(resolved.body)
                logger.debug("Expanded @%s in chained prompt", cmd_name)
            else:
                return None
            break  # @command consumes remaining tokens
        else:
            # Plain text — append rest as-is
            blocks.append(" ".join(tokens[i:]))
            break

    if not blocks:
        return None

    return "\n\n---\n\n".join(blocks)
