"""
obscura.core.supervisor.prompt_assembler — Deterministic prompt assembly.

Assembles a prompt from ordered sections, freezes it as a PromptSnapshot,
and ensures stability across turns within a run.

Section ordering is fixed:
    1. SYSTEM_PROMPT
    2. CONTEXT_INSTRUCTIONS
    3. AGENT_DEFINITION
    4. TOOL_DEFINITIONS
    5. MEMORY_SNIPPETS
    6. SESSION_HISTORY
    7. HOOK_INJECTIONS
    8. USER_PROMPT
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from obscura.core.supervisor.errors import DriftDetectedError, PromptAssemblyError
from obscura.core.supervisor.types import PromptSection, PromptSnapshot

logger = logging.getLogger(__name__)

# Section names in canonical order (never reorder)
SECTION_ORDER: tuple[str, ...] = (
    "system_prompt",
    "context_instructions",
    "agent_definition",
    "tool_definitions",
    "memory_snippets",
    "session_history",
    "hook_injections",
    "user_prompt",
)


class PromptAssembler:
    """Assembles deterministic, hashable prompt snapshots.

    Each run gets exactly one snapshot, created during BUILDING_CONTEXT.
    The snapshot is frozen and reused for every turn in the run.

    Usage::

        assembler = PromptAssembler(token_budget=100000)
        assembler.set_section("system_prompt", "You are a helpful assistant.")
        assembler.set_section("user_prompt", "Fix the bug in auth.py")
        assembler.set_section("tool_definitions", tool_defs_text)
        assembler.set_section("memory_snippets", memory_text)

        snapshot = assembler.freeze()
        # snapshot.prompt_hash is stable
        # snapshot.sections is immutable
    """

    def __init__(
        self,
        *,
        token_budget: int = 0,
        reserved_output_tokens: int = 4096,
        chars_per_token: float = 4.0,
        store_full_prompt: bool = True,
    ) -> None:
        self._sections: dict[str, str] = {}
        self._token_budget = token_budget
        self._reserved_output = reserved_output_tokens
        self._chars_per_token = chars_per_token
        self._store_full_prompt = store_full_prompt
        self._frozen: PromptSnapshot | None = None

    # -- section management --------------------------------------------------

    def set_section(self, name: str, content: str) -> None:
        """Set a prompt section by name.

        Raises:
            PromptAssemblyError: If the section name is not recognized.
        """
        if name not in SECTION_ORDER:
            raise PromptAssemblyError(
                f"Unknown section: {name!r}. "
                f"Valid sections: {', '.join(SECTION_ORDER)}"
            )
        if self._frozen is not None:
            raise PromptAssemblyError(
                "Cannot modify sections after freeze(). "
                "Create a new assembler for a new run."
            )
        self._sections[name] = content

    def get_section(self, name: str) -> str:
        """Get a section's content (empty string if not set)."""
        return self._sections.get(name, "")

    # -- freezing ------------------------------------------------------------

    def freeze(self) -> PromptSnapshot:
        """Freeze all sections into an immutable snapshot.

        Applies token budget trimming to session_history only.
        Computes the prompt hash for fingerprinting.

        Returns:
            Frozen PromptSnapshot.

        Raises:
            PromptAssemblyError: If required sections are missing.
        """
        if self._frozen is not None:
            return self._frozen

        # Validate required sections
        if not self._sections.get("user_prompt"):
            raise PromptAssemblyError("user_prompt section is required")

        # Build sections in canonical order
        sections: list[PromptSection] = []
        for name in SECTION_ORDER:
            content = self._sections.get(name, "")
            if not content:
                continue
            token_est = self._estimate_tokens(content)
            sections.append(
                PromptSection(name=name, content=content, token_estimate=token_est)
            )

        # Apply token budget (trim session_history from oldest)
        if self._token_budget > 0:
            sections = self._apply_budget(sections)

        # Compute hash
        hash_input = "\n".join(
            f"[{s.name}]\n{s.content}" for s in sections
        )
        prompt_hash = hashlib.sha256(hash_input.encode()).hexdigest()

        total_tokens = sum(s.token_estimate for s in sections)

        self._frozen = PromptSnapshot(
            sections=tuple(sections),
            prompt_hash=prompt_hash,
            total_tokens=total_tokens,
        )

        logger.debug(
            "Prompt frozen: hash=%s, sections=%d, tokens=%d",
            prompt_hash[:12],
            len(sections),
            total_tokens,
        )
        return self._frozen

    @property
    def snapshot(self) -> PromptSnapshot | None:
        """The frozen snapshot (None if not yet frozen)."""
        return self._frozen

    # -- assembled prompt text -----------------------------------------------

    def assemble_text(self) -> str:
        """Return the full assembled prompt as text.

        Freezes first if not already frozen.
        """
        snapshot = self._frozen or self.freeze()
        return "\n\n".join(s.content for s in snapshot.sections)

    # -- drift detection -----------------------------------------------------

    def check_drift(self, expected_hash: str) -> None:
        """Check if current snapshot hash matches expected.

        Raises:
            DriftDetectedError: If hashes don't match.
        """
        if self._frozen is None:
            raise PromptAssemblyError("Cannot check drift before freeze()")
        if self._frozen.prompt_hash != expected_hash:
            raise DriftDetectedError(
                "prompt",
                expected=expected_hash,
                actual=self._frozen.prompt_hash,
            )

    # -- internal ------------------------------------------------------------

    def _estimate_tokens(self, text: str) -> int:
        """Rough token estimate (chars / chars_per_token)."""
        return max(1, int(len(text) / self._chars_per_token))

    def _apply_budget(
        self, sections: list[PromptSection]
    ) -> list[PromptSection]:
        """Trim session_history to fit within token budget.

        Budget allocation:
        1. Fixed sections (system, context, agent, tools, memory, hooks, user) are untouched
        2. session_history is trimmed from oldest messages to fit
        """
        budget = self._token_budget - self._reserved_output
        if budget <= 0:
            return sections

        # Sum non-history tokens
        fixed_tokens = sum(
            s.token_estimate for s in sections if s.name != "session_history"
        )
        available_for_history = budget - fixed_tokens

        if available_for_history <= 0:
            # No room for history at all — remove it
            return [s for s in sections if s.name != "session_history"]

        # Find and potentially trim the history section
        result = []
        for section in sections:
            if section.name == "session_history":
                if section.token_estimate <= available_for_history:
                    result.append(section)
                else:
                    # Trim from the beginning (oldest messages)
                    trimmed = self._trim_history(
                        section.content, available_for_history
                    )
                    if trimmed:
                        result.append(
                            PromptSection(
                                name="session_history",
                                content=trimmed,
                                token_estimate=self._estimate_tokens(trimmed),
                            )
                        )
            else:
                result.append(section)

        return result

    def _trim_history(self, history: str, token_budget: int) -> str:
        """Trim history from oldest (top) to fit budget.

        Splits on double-newline (message boundaries) and drops oldest
        messages until within budget.
        """
        char_budget = int(token_budget * self._chars_per_token)
        if len(history) <= char_budget:
            return history

        # Split into messages and keep newest
        messages = history.split("\n\n")
        result: list[str] = []
        total = 0

        # Work backwards (newest first)
        for msg in reversed(messages):
            if total + len(msg) + 2 > char_budget:
                break
            result.append(msg)
            total += len(msg) + 2

        if not result:
            # At least keep the last message, even if over budget
            return messages[-1]

        result.reverse()
        return "\n\n".join(result)


# ---------------------------------------------------------------------------
# Utility: format tool definitions for prompt injection
# ---------------------------------------------------------------------------


def format_tool_definitions(tools: list[dict[str, Any]]) -> str:
    """Format tool definitions for prompt injection.

    Sorted by name for deterministic ordering.
    """
    sorted_tools = sorted(tools, key=lambda t: t.get("name", ""))
    parts = []
    for tool in sorted_tools:
        name = tool.get("name", "unknown")
        desc = tool.get("description", "")
        params = json.dumps(tool.get("parameters", {}), indent=2, sort_keys=True)
        parts.append(f"### {name}\n{desc}\n\nParameters:\n```json\n{params}\n```")
    return "\n\n".join(parts)
