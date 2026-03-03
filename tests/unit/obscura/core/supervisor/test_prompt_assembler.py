"""Tests for deterministic prompt assembly."""

from __future__ import annotations

import pytest

from obscura.core.supervisor.errors import DriftDetectedError, PromptAssemblyError
from obscura.core.supervisor.prompt_assembler import (
    SECTION_ORDER,
    PromptAssembler,
    format_tool_definitions,
)


class TestPromptAssembler:
    """Prompt assembly must be deterministic and stable."""

    def test_basic_assembly(self) -> None:
        asm = PromptAssembler()
        asm.set_section("system_prompt", "You are helpful.")
        asm.set_section("user_prompt", "Hello")
        snapshot = asm.freeze()
        assert len(snapshot.sections) == 2
        assert snapshot.prompt_hash  # non-empty
        assert snapshot.total_tokens > 0

    def test_section_ordering_is_fixed(self) -> None:
        asm = PromptAssembler()
        # Set sections in reverse order
        asm.set_section("user_prompt", "Hello")
        asm.set_section("system_prompt", "System")
        asm.set_section("memory_snippets", "Memory")
        snapshot = asm.freeze()
        names = [s.name for s in snapshot.sections]
        # Must follow SECTION_ORDER regardless of insertion order
        assert names == ["system_prompt", "memory_snippets", "user_prompt"]

    def test_deterministic_hash(self) -> None:
        """Same inputs → same hash."""
        for _ in range(3):
            asm = PromptAssembler()
            asm.set_section("system_prompt", "You are helpful.")
            asm.set_section("user_prompt", "Fix the bug")
            snapshot = asm.freeze()
            assert snapshot.prompt_hash == asm.freeze().prompt_hash

    def test_hash_changes_with_content(self) -> None:
        asm1 = PromptAssembler()
        asm1.set_section("user_prompt", "Hello")
        h1 = asm1.freeze().prompt_hash

        asm2 = PromptAssembler()
        asm2.set_section("user_prompt", "Goodbye")
        h2 = asm2.freeze().prompt_hash

        assert h1 != h2

    def test_unknown_section_raises(self) -> None:
        asm = PromptAssembler()
        with pytest.raises(PromptAssemblyError, match="Unknown section"):
            asm.set_section("invalid_section", "content")

    def test_missing_user_prompt_raises(self) -> None:
        asm = PromptAssembler()
        asm.set_section("system_prompt", "System")
        with pytest.raises(PromptAssemblyError, match="user_prompt.*required"):
            asm.freeze()

    def test_cannot_modify_after_freeze(self) -> None:
        asm = PromptAssembler()
        asm.set_section("user_prompt", "Hello")
        asm.freeze()
        with pytest.raises(PromptAssemblyError, match="Cannot modify"):
            asm.set_section("system_prompt", "New system")

    def test_freeze_is_idempotent(self) -> None:
        asm = PromptAssembler()
        asm.set_section("user_prompt", "Hello")
        s1 = asm.freeze()
        s2 = asm.freeze()
        assert s1 is s2

    def test_drift_detection(self) -> None:
        asm = PromptAssembler()
        asm.set_section("user_prompt", "Hello")
        snapshot = asm.freeze()
        asm.check_drift(snapshot.prompt_hash)  # should not raise
        with pytest.raises(DriftDetectedError):
            asm.check_drift("wrong_hash")

    def test_token_budget_trims_history(self) -> None:
        asm = PromptAssembler(token_budget=50, reserved_output_tokens=10)
        asm.set_section("system_prompt", "System")  # ~2 tokens
        asm.set_section("user_prompt", "Hello")  # ~2 tokens
        # Create history with message boundaries so trimming can split
        messages = [f"Message {i}: " + "x" * 40 for i in range(10)]
        history = "\n\n".join(messages)  # ~500 chars, ~125 tokens
        asm.set_section("session_history", history)
        snapshot = asm.freeze()
        # History should be trimmed (budget=40 tokens, fixed~4, history gets ~36)
        history_sections = [s for s in snapshot.sections if s.name == "session_history"]
        if history_sections:
            assert len(history_sections[0].content) < len(history)

    def test_assemble_text(self) -> None:
        asm = PromptAssembler()
        asm.set_section("system_prompt", "System")
        asm.set_section("user_prompt", "Hello")
        text = asm.assemble_text()
        assert "System" in text
        assert "Hello" in text

    def test_empty_sections_excluded(self) -> None:
        asm = PromptAssembler()
        asm.set_section("user_prompt", "Hello")
        asm.set_section("system_prompt", "")  # empty
        snapshot = asm.freeze()
        names = [s.name for s in snapshot.sections]
        assert "system_prompt" not in names


class TestFormatToolDefinitions:
    def test_sorted_by_name(self) -> None:
        tools = [
            {"name": "zebra", "description": "Z tool", "parameters": {}},
            {"name": "alpha", "description": "A tool", "parameters": {}},
        ]
        result = format_tool_definitions(tools)
        assert result.index("alpha") < result.index("zebra")

    def test_includes_schema(self) -> None:
        tools = [
            {
                "name": "bash",
                "description": "Run command",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            },
        ]
        result = format_tool_definitions(tools)
        assert "bash" in result
        assert "Run command" in result
        assert '"cmd"' in result
