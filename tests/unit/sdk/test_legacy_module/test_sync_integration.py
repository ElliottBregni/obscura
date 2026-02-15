"""Integration tests: Domain 1 — in-repo recursive sync with per-file symlinks."""

from __future__ import annotations

from pathlib import Path

from scripts.sync import VaultSync


class TestDomain1InRepoSync:
    """Domain 1: sync_all() creates agent dirs with per-file symlinks in repo targets."""

    def test_sync_creates_real_dirs(
        self, sync_instance: VaultSync, mock_repo: Path, mock_home: Path
    ) -> None:
        """sync_all creates real directories at all discovered targets."""
        sync_instance.sync_all()

        assert (mock_repo / ".github").is_dir() and not (
            mock_repo / ".github"
        ).is_symlink(), ".github should be a real directory at root"
        assert (mock_repo / ".claude").is_dir() and not (
            mock_repo / ".claude"
        ).is_symlink(), ".claude should be a real directory at root"
        assert (mock_repo / "platform" / ".github").is_dir(), (
            ".github should exist in platform/"
        )
        assert (mock_repo / "platform" / ".claude").is_dir(), (
            ".claude should exist in platform/"
        )
        assert (mock_repo / "platform" / "partview_core" / ".github").is_dir(), (
            ".github should exist in platform/partview_core/"
        )
        assert (mock_repo / "platform" / "partview_core" / ".claude").is_dir(), (
            ".claude should exist in platform/partview_core/"
        )

    def test_sync_agent_filtering(
        self, sync_instance: VaultSync, mock_repo: Path, mock_home: Path
    ) -> None:
        """Copilot sees copilot-instructions.md, Claude does not."""
        sync_instance.sync_all()

        assert (mock_repo / ".github" / "copilot-instructions.md").exists(), (
            ".github should have copilot-instructions.md"
        )
        assert not (mock_repo / ".claude" / "copilot-instructions.md").exists(), (
            ".claude should NOT have copilot-instructions.md"
        )

    def test_sync_universal_files(
        self, sync_instance: VaultSync, mock_repo: Path, mock_home: Path
    ) -> None:
        """Both agents see universal files."""
        sync_instance.sync_all()

        assert (mock_repo / ".github" / "agent.md").exists(), (
            ".github should have agent.md"
        )
        assert (mock_repo / ".claude" / "agent.md").exists(), (
            ".claude should have agent.md"
        )

    def test_sync_symlinks_point_to_vault(
        self,
        sync_instance: VaultSync,
        vault_root: Path,
        mock_repo: Path,
        mock_home: Path,
    ) -> None:
        """Symlinks in target dir point back to vault."""
        sync_instance.sync_all()

        link = mock_repo / ".github" / "agent.md"
        assert link.is_symlink(), "agent.md should be a symlink"
        target = link.resolve()
        assert str(vault_root) in str(target), (
            f"Symlink should point to vault, got {target}"
        )

    def test_repo_no_vault_content(
        self, sync_instance: VaultSync, mock_repo: Path, mock_home: Path
    ) -> None:
        """Repo agent dirs do NOT contain vault-wide content."""
        sync_instance.sync_all()

        github = mock_repo / ".github"
        assert not (github / "docs").exists(), (
            ".github should NOT have docs/ (vault-wide content)"
        )
        assert not (github / "skills" / "git-workflow.md").exists(), (
            ".github should NOT have vault skill git-workflow.md"
        )
        assert not (github / "skills" / "testing.md").exists(), (
            ".github should NOT have vault skill testing.md"
        )

    def test_repo_has_repo_skills(
        self, sync_instance: VaultSync, mock_repo: Path, mock_home: Path
    ) -> None:
        """Repo agent dirs DO contain repo-specific skills."""
        sync_instance.sync_all()

        github = mock_repo / ".github"
        assert (github / "skills" / "subagent" / "Models" / "Skill.md").exists(), (
            ".github should have repo skill subagent/Models/Skill.md"
        )
        assert (github / "skills" / "changelog-generator" / "SKILL.md").exists(), (
            ".github should have repo skill changelog-generator/SKILL.md"
        )

    def test_root_excludes_platform_content(
        self, sync_instance: VaultSync, mock_repo: Path, mock_home: Path
    ) -> None:
        """Root .github should NOT contain platform/ subdirectory content."""
        sync_instance.sync_all()

        github = mock_repo / ".github"
        assert not (github / "platform").exists(), (
            "Root .github should NOT have platform/ dir"
        )

    def test_platform_has_scoped_skills(
        self, sync_instance: VaultSync, mock_repo: Path, mock_home: Path
    ) -> None:
        """platform/.github has scoped skills + inherited root files."""
        sync_instance.sync_all()

        plat_github = mock_repo / "platform" / ".github"
        plat_files = sorted(
            str(f.relative_to(plat_github))
            for f in plat_github.rglob("*")
            if f.is_symlink()
        )

        # Scoped skills
        assert any("skills/subagent" in f for f in plat_files), (
            f"platform/.github should have subagent skill, got: {plat_files}"
        )

        # Inherited root-level files
        assert "agent.md" in plat_files, (
            "platform/.github should have inherited agent.md"
        )

        # No vault-wide content
        assert not any("docs/" in f for f in plat_files), (
            f"platform/.github should NOT have vault docs"
        )

    def test_platform_excludes_partview_content(
        self, sync_instance: VaultSync, mock_repo: Path, mock_home: Path
    ) -> None:
        """platform/.github should NOT contain partview_core/ content."""
        sync_instance.sync_all()

        plat_github = mock_repo / "platform" / ".github"
        assert not (plat_github / "skills" / "partview_core").exists(), (
            "platform/.github should NOT have partview_core/ content (it's a separate target)"
        )

    def test_partview_has_own_skills(
        self, sync_instance: VaultSync, mock_repo: Path, mock_home: Path
    ) -> None:
        """platform/partview_core/.github has its own skills + inherited root files."""
        sync_instance.sync_all()

        pv_github = mock_repo / "platform" / "partview_core" / ".github"
        pv_files = sorted(
            str(f.relative_to(pv_github))
            for f in pv_github.rglob("*")
            if f.is_symlink()
        )

        # Own skills
        assert any("skills/" in f for f in pv_files), (
            f"partview_core/.github should have skills, got {pv_files}"
        )

        # Inherited root-level files
        assert "agent.md" in pv_files, "partview_core/.github should inherit agent.md"

    def test_partview_no_platform_skills(
        self,
        sync_instance: VaultSync,
        vault_root: Path,
        mock_repo: Path,
        mock_home: Path,
    ) -> None:
        """partview_core/.github should NOT have platform's skills (no cascading)."""
        sync_instance.sync_all()

        pv_github = mock_repo / "platform" / "partview_core" / ".github"
        for link in pv_github.rglob("*"):
            if link.is_symlink():
                source = str(link.resolve())
                rel_to_pv = str(link.relative_to(pv_github))
                if rel_to_pv not in (
                    "agent.md",
                    "agent.json",
                    "config.json",
                    "copilot-instructions.md",
                ):
                    if "skills" in rel_to_pv:
                        assert "partview_core" in source or "TestRepo/" in source, (
                            f"partview_core skill {rel_to_pv} should point to partview_core vault, got {source}"
                        )

    def test_sync_idempotent(
        self,
        sync_instance: VaultSync,
        vault_root: Path,
        mock_repo: Path,
        mock_home: Path,
    ) -> None:
        """Running sync twice produces same result."""
        vault_repo = vault_root / "repos" / "TestRepo"
        targets = sync_instance.discover_sync_targets(vault_repo, mock_repo)
        root = [t for t in targets if t.repo_path == mock_repo][0]

        sync_instance.sync_target("copilot", root, vault_repo, mock_repo)
        count1 = sum(1 for _ in (mock_repo / ".github").rglob("*") if _.is_symlink())

        sync_instance.sync_target("copilot", root, vault_repo, mock_repo)
        count2 = sum(1 for _ in (mock_repo / ".github").rglob("*") if _.is_symlink())

        assert count1 == count2, f"Idempotent check: {count1} != {count2}"
