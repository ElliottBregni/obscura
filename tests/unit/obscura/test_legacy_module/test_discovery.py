"""Tests for VaultSync.discover_sync_targets() — recursive directory matching."""

from __future__ import annotations

from pathlib import Path

from scripts.sync import VaultSync


class TestRecursiveDiscovery:
    """Recursive target discovery: matching vault dirs against real repo dirs."""

    def test_discover_targets_finds_root(
        self, sync_instance: VaultSync, vault_root: Path, mock_repo: Path
    ) -> None:
        """Discovery finds the repo root as a target."""
        vault_repo = vault_root / "repos" / "TestRepo"
        targets = sync_instance.discover_sync_targets(vault_repo, mock_repo)

        repo_paths = [t.repo_path for t in targets]
        assert mock_repo in repo_paths, f"Root should be a target, got {repo_paths}"

    def test_discover_targets_finds_platform(
        self, sync_instance: VaultSync, vault_root: Path, mock_repo: Path
    ) -> None:
        """Discovery finds platform/ as a target (vault has platform/, repo has platform/)."""
        vault_repo = vault_root / "repos" / "TestRepo"
        targets = sync_instance.discover_sync_targets(vault_repo, mock_repo)

        repo_paths = [t.repo_path for t in targets]
        assert (mock_repo / "platform") in repo_paths, (
            f"platform/ should be a discovered target, got {[str(p) for p in repo_paths]}"
        )

    def test_discover_targets_finds_partview_core(
        self, sync_instance: VaultSync, vault_root: Path, mock_repo: Path
    ) -> None:
        """Discovery finds platform/partview_core/ (vault has platform/skills/partview_core/,
        repo has platform/partview_core/)."""
        vault_repo = vault_root / "repos" / "TestRepo"
        targets = sync_instance.discover_sync_targets(vault_repo, mock_repo)

        repo_paths = [t.repo_path for t in targets]
        assert (mock_repo / "platform" / "partview_core") in repo_paths, (
            f"platform/partview_core/ should be a discovered target, got {[str(p) for p in repo_paths]}"
        )

    def test_discover_targets_count(
        self, sync_instance: VaultSync, vault_root: Path, mock_repo: Path
    ) -> None:
        """Should discover exactly 3 targets: root, platform/, platform/partview_core/."""
        vault_repo = vault_root / "repos" / "TestRepo"
        targets = sync_instance.discover_sync_targets(vault_repo, mock_repo)

        assert len(targets) == 3, (
            f"Expected 3 targets, got {len(targets)}: {[str(t.repo_path) for t in targets]}"
        )

    def test_root_target_has_files(
        self, sync_instance: VaultSync, vault_root: Path, mock_repo: Path
    ) -> None:
        """Root target should have root-level files and skills content."""
        vault_repo = vault_root / "repos" / "TestRepo"
        targets = sync_instance.discover_sync_targets(vault_repo, mock_repo)

        root = [t for t in targets if t.repo_path == mock_repo][0]
        dest_names = [str(d) for _, d in root.files]

        assert "agent.md" in dest_names, f"Root should have agent.md, got {dest_names}"
        assert any("skills/" in d for d in dest_names), (
            f"Root should have skills content, got {dest_names}"
        )
        # Should NOT have platform-level content
        assert not any(d.startswith("platform/") for d in dest_names), (
            f"Root should NOT have platform/ content, got {dest_names}"
        )

    def test_platform_target_has_skills(
        self, sync_instance: VaultSync, vault_root: Path, mock_repo: Path
    ) -> None:
        """Platform target should have skills from vault platform/skills/."""
        vault_repo = vault_root / "repos" / "TestRepo"
        targets = sync_instance.discover_sync_targets(vault_repo, mock_repo)

        plat = [t for t in targets if t.repo_path == mock_repo / "platform"][0]
        dest_names = [str(d) for _, d in plat.files]

        assert any("skills/" in d for d in dest_names), (
            f"Platform target should have skills, got {dest_names}"
        )
        # Should NOT have partview_core content
        assert not any("partview_core" in d for d in dest_names), (
            f"Platform target should NOT have partview_core content, got {dest_names}"
        )

    def test_partview_target_has_own_skills(
        self, sync_instance: VaultSync, vault_root: Path, mock_repo: Path
    ) -> None:
        """Partview target should have its own skills from vault."""
        vault_repo = vault_root / "repos" / "TestRepo"
        targets = sync_instance.discover_sync_targets(vault_repo, mock_repo)

        pv = [
            t
            for t in targets
            if t.repo_path == mock_repo / "platform" / "partview_core"
        ][0]
        dest_names = [str(d) for _, d in pv.files]

        assert any("skills/" in d for d in dest_names), (
            f"Partview target should have its own skills, got {dest_names}"
        )
