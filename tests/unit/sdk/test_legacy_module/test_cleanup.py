"""Integration tests: cleanup — remove_links, remove_system_links, remove_all."""

from __future__ import annotations

from pathlib import Path

from scripts.sync import VaultSync


class TestCleanup:
    """Cleanup: removing agent dirs, system links, and full remove_all."""

    def test_remove_links(
        self,
        sync_instance: VaultSync,
        vault_root: Path,
        mock_repo: Path,
        mock_home: Path,
    ) -> None:
        """Remove links cleans up repo agent directories."""
        vault_repo = vault_root / "repos" / "TestRepo"
        targets = sync_instance.discover_sync_targets(vault_repo, mock_repo)
        root = [t for t in targets if t.repo_path == mock_repo][0]

        sync_instance.sync_target("copilot", root, vault_repo, mock_repo)
        assert (mock_repo / ".github").exists()

        sync_instance.remove_links(mock_repo)

        assert not (mock_repo / ".github").exists(), ".github should be removed"
        assert not (mock_repo / ".claude").exists(), ".claude should be removed"

    def test_remove_system_links(
        self, sync_instance: VaultSync, mock_home: Path
    ) -> None:
        """remove_system_links cleans vault-managed content from system dirs."""
        sync_instance.sync_system()

        github = mock_home / ".github"
        assert (github / "skills").exists(), (
            "~/.github/skills/ should exist before removal"
        )

        sync_instance.remove_system_links()

        assert not (github / "skills").exists(), "~/.github/skills/ should be removed"
        assert not (github / "docs").exists(), "~/.github/docs/ should be removed"
        assert not (github / "instructions").exists(), (
            "~/.github/instructions/ should be removed"
        )

    def test_remove_all_cleans_everything(
        self, sync_instance: VaultSync, mock_repo: Path, mock_home: Path
    ) -> None:
        """remove_all cleans repos + subdirs + system-level agent dirs."""
        sync_instance.sync_all()

        assert (mock_repo / ".github").exists()
        assert (mock_repo / "platform" / ".github").exists()
        assert (mock_repo / "platform" / "partview_core" / ".github").exists()
        assert (mock_home / ".github" / "skills").exists()

        sync_instance.remove_all()

        assert not (mock_repo / ".github").exists(), "Root .github should be removed"
        assert not (mock_repo / ".claude").exists(), "Root .claude should be removed"
        assert not (mock_repo / "platform" / ".github").exists(), (
            "platform/.github should be removed"
        )
        assert not (mock_repo / "platform" / ".claude").exists(), (
            "platform/.claude should be removed"
        )
        assert not (mock_repo / "platform" / "partview_core" / ".github").exists(), (
            "partview_core/.github should be removed"
        )
        assert not (mock_repo / "platform" / "partview_core" / ".claude").exists(), (
            "partview_core/.claude should be removed"
        )
        assert not (mock_home / ".github" / "skills").exists(), (
            "~/.github/skills/ should be removed"
        )
