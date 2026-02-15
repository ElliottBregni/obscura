"""Integration tests: Domain 2 — system-level vault-wide content sync."""

from __future__ import annotations

from pathlib import Path

from scripts.sync import VaultSync


class TestDomain2SystemSync:
    """Domain 2: sync_system() creates vault-wide content in ~/{agent_target}/."""

    def test_sync_system_creates_symlinks(
        self, sync_instance: VaultSync, mock_home: Path
    ) -> None:
        """sync_system creates vault-wide content in system agent dirs."""
        sync_instance.sync_system()

        github = mock_home / ".github"
        assert (github / "skills" / "git-workflow.md").exists(), (
            "~/.github/skills/git-workflow.md should exist"
        )
        assert (github / "skills" / "testing.md").exists(), (
            "~/.github/skills/testing.md should exist"
        )
        assert (github / "docs").is_dir(), "~/.github/docs/ should exist"
        assert (github / "instructions").is_dir(), (
            "~/.github/instructions/ should exist"
        )

    def test_sync_system_nested_override(
        self, sync_instance: VaultSync, mock_home: Path
    ) -> None:
        """Nested override: setup.copilot.md appears as setup.md in ~/.github/."""
        sync_instance.sync_system()

        link = mock_home / ".github" / "skills" / "setup.md"
        assert link.exists(), "setup.md should exist in ~/.github/skills/"
        assert link.is_symlink(), "setup.md should be a symlink"
        target = str(link.resolve())
        assert "setup.copilot.md" in target, (
            f"setup.md should point to setup.copilot.md, got {target}"
        )

    def test_sync_system_agent_dir_content(
        self, sync_instance: VaultSync, mock_home: Path
    ) -> None:
        """Agent dir: skills.copilot/python.md -> ~/.github/skills/python.md"""
        sync_instance.sync_system()

        link = mock_home / ".github" / "skills" / "python.md"
        assert link.exists(), "python.md should exist in ~/.github/skills/"
        target = str(link.resolve())
        assert "skills.copilot" in target, (
            f"python.md should point to skills.copilot/, got {target}"
        )

    def test_sync_system_agent_filtering(
        self, sync_instance: VaultSync, mock_home: Path
    ) -> None:
        """Each agent gets its own filtered content at system level."""
        sync_instance.sync_system()

        github = mock_home / ".github"
        claude = mock_home / ".claude"

        assert (github / "skills" / "api-design.md").exists(), (
            "~/.github should have api-design.md (copilot)"
        )

        assert (claude / "skills" / "database.md").exists(), (
            "~/.claude should have database.md (claude)"
        )
        assert not (claude / "skills" / "api-design.md").exists(), (
            "~/.claude should NOT have api-design.md (copilot-only)"
        )

    def test_sync_system_preserves_non_managed_files(
        self, sync_instance: VaultSync, mock_home: Path
    ) -> None:
        """Sync and cleanup preserve non-managed files in ~/.claude/."""
        claude = mock_home / ".claude"
        claude.mkdir(exist_ok=True)

        settings = claude / "settings.local.json"
        settings.write_text('{"key": "value"}\n')

        sync_instance.sync_system()
        sync_instance.remove_system_links()
        sync_instance.sync_system()

        assert settings.exists(), "~/.claude/settings.local.json should be preserved"


# pyright: ignore-all
