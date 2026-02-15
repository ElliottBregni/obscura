"""Tests for sdk.context — ContextLoader."""

from __future__ import annotations


import pytest

from pathlib import Path

from sdk.internal.types import Backend
from sdk.context import ContextLoader


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Create a mock vault directory structure."""
    # Copilot dirs
    gh = tmp_path / ".github"
    (gh / "instructions").mkdir(parents=True)
    (gh / "skills").mkdir(parents=True)
    (gh / "skills" / "roles" / "architect").mkdir(parents=True)

    # Write some content
    (gh / "instructions" / "setup.md").write_text("# Setup\nDo the thing.")
    (gh / "instructions" / "code-style.md").write_text("# Code Style\nUse black.")
    (gh / "skills" / "python.md").write_text("# Python\nUse type hints.")
    (gh / "skills" / "api-design.md").write_text("# API Design\nREST first.")
    (gh / "skills" / "roles" / "architect" / "travelers.opus.md").write_text(
        "# Architect Role\nAnalyze deeply."
    )

    # Claude dirs
    claude = tmp_path / ".claude"
    (claude / "instructions").mkdir(parents=True)
    (claude / "instructions" / "setup.md").write_text("# Claude Setup\nBe concise.")

    return tmp_path


# ---------------------------------------------------------------------------
# agent_dir
# ---------------------------------------------------------------------------


class TestAgentDir:
    def test_copilot_agent_dir(self, vault: Path):
        loader = ContextLoader(Backend.COPILOT, vault_path=vault)
        assert loader.agent_dir == vault / ".github"

    def test_claude_agent_dir(self, vault: Path):
        loader = ContextLoader(Backend.CLAUDE, vault_path=vault)
        assert loader.agent_dir == vault / ".claude"


# ---------------------------------------------------------------------------
# load_instructions
# ---------------------------------------------------------------------------


class TestLoadInstructions:
    def test_loads_all_instruction_files(self, vault: Path):
        loader = ContextLoader(Backend.COPILOT, vault_path=vault)
        instructions = loader.load_instructions()
        assert "# Setup" in instructions
        assert "# Code Style" in instructions
        assert "---" in instructions  # separator

    def test_returns_empty_for_missing_dir(self, tmp_path: Path):
        loader = ContextLoader(Backend.COPILOT, vault_path=tmp_path)
        assert loader.load_instructions() == ""

    def test_claude_instructions(self, vault: Path):
        loader = ContextLoader(Backend.CLAUDE, vault_path=vault)
        instructions = loader.load_instructions()
        assert "# Claude Setup" in instructions


# ---------------------------------------------------------------------------
# load_skills
# ---------------------------------------------------------------------------


class TestLoadSkills:
    def test_loads_skill_files(self, vault: Path):
        loader = ContextLoader(Backend.COPILOT, vault_path=vault)
        skills = loader.load_skills()
        texts = "\n".join(skills)
        assert "# Python" in texts
        assert "# API Design" in texts

    def test_returns_empty_list_for_missing_dir(self, tmp_path: Path):
        loader = ContextLoader(Backend.COPILOT, vault_path=tmp_path)
        assert loader.load_skills() == []

    def test_skips_empty_files(self, vault: Path):
        # Create an empty skill file
        (vault / ".github" / "skills" / "empty.md").write_text("")
        loader = ContextLoader(Backend.COPILOT, vault_path=vault)
        skills = loader.load_skills()
        for s in skills:
            assert s.strip()  # no empty strings


# ---------------------------------------------------------------------------
# load_role
# ---------------------------------------------------------------------------


class TestLoadRole:
    def test_loads_role_context(self, vault: Path):
        loader = ContextLoader(Backend.COPILOT, vault_path=vault)
        role = loader.load_role("architect")
        assert "# Architect Role" in role
        assert "Analyze deeply" in role

    def test_returns_empty_for_missing_role(self, vault: Path):
        loader = ContextLoader(Backend.COPILOT, vault_path=vault)
        assert loader.load_role("nonexistent") == ""


# ---------------------------------------------------------------------------
# load_system_prompt
# ---------------------------------------------------------------------------


class TestLoadSystemPrompt:
    def test_combines_instructions_and_skills(self, vault: Path):
        loader = ContextLoader(Backend.COPILOT, vault_path=vault)
        prompt = loader.load_system_prompt()
        assert "# Setup" in prompt
        assert "## Skills" in prompt
        assert "# Python" in prompt

    def test_additional_content(self, vault: Path):
        loader = ContextLoader(Backend.COPILOT, vault_path=vault)
        prompt = loader.load_system_prompt(additional="Extra context here.")
        assert "Extra context here." in prompt

    def test_empty_vault(self, tmp_path: Path):
        loader = ContextLoader(Backend.COPILOT, vault_path=tmp_path)
        assert loader.load_system_prompt() == ""
