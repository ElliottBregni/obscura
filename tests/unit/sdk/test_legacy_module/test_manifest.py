"""Tests for manifest building — agent-filtered file manifests."""

from __future__ import annotations

from pathlib import Path

from scripts.sync import VaultSync


class TestManifestBuilding:
    """Manifest building: per-target and vault-wide agent filtering."""

    def test_build_target_manifest_copilot(
        self, sync_instance: VaultSync, vault_root: Path, mock_repo: Path
    ) -> None:
        """Manifest for root target has copilot-specific files."""
        vault_repo = vault_root / "repos" / "TestRepo"
        targets = sync_instance.discover_sync_targets(vault_repo, mock_repo)
        root = [t for t in targets if t.repo_path == mock_repo][0]

        manifest = sync_instance.build_target_manifest(
            "copilot", root, vault_repo, mock_repo
        )

        assert Path("copilot-instructions.md") in manifest, \
            "copilot manifest should have copilot-instructions.md"
        assert Path("agent.md") in manifest, \
            "copilot manifest should have agent.md"

        # Nested override stripped
        assert Path("skills/subagent/Models/skill.md") in manifest, \
            "skill.md (from skill.copilot.md) should be in manifest"
        source = manifest[Path("skills/subagent/Models/skill.md")]
        assert "skill.copilot.md" in str(source), \
            f"skill.md should point to skill.copilot.md, got {source}"

    def test_build_target_manifest_claude(
        self, sync_instance: VaultSync, vault_root: Path, mock_repo: Path
    ) -> None:
        """Manifest for root target filters copilot-specific files."""
        vault_repo = vault_root / "repos" / "TestRepo"
        targets = sync_instance.discover_sync_targets(vault_repo, mock_repo)
        root = [t for t in targets if t.repo_path == mock_repo][0]

        manifest = sync_instance.build_target_manifest(
            "claude", root, vault_repo, mock_repo
        )

        assert Path("copilot-instructions.md") not in manifest, \
            "Claude should not have copilot-instructions.md"
        assert Path("agent.md") in manifest, \
            "Claude should have agent.md"

    def test_build_target_manifest_no_vault_content(
        self, sync_instance: VaultSync, vault_root: Path, mock_repo: Path
    ) -> None:
        """Root target manifest has NO vault-wide content."""
        vault_repo = vault_root / "repos" / "TestRepo"
        targets = sync_instance.discover_sync_targets(vault_repo, mock_repo)
        root = [t for t in targets if t.repo_path == mock_repo][0]

        manifest = sync_instance.build_target_manifest(
            "copilot", root, vault_repo, mock_repo
        )

        assert Path("skills/git-workflow.md") not in manifest, \
            "Vault skill should NOT be in repo manifest"
        assert Path("docs/AUTO-SYNC.md") not in manifest, \
            "Vault docs should NOT be in repo manifest"

    def test_child_target_inherits_root_files(
        self, sync_instance: VaultSync, vault_root: Path, mock_repo: Path
    ) -> None:
        """Non-root targets inherit root-level files."""
        vault_repo = vault_root / "repos" / "TestRepo"
        targets = sync_instance.discover_sync_targets(vault_repo, mock_repo)
        plat = [t for t in targets if t.repo_path == mock_repo / "platform"][0]

        manifest = sync_instance.build_target_manifest(
            "copilot", plat, vault_repo, mock_repo
        )

        assert Path("agent.md") in manifest, \
            "platform target should inherit agent.md from root"
        assert Path("config.json") in manifest, \
            "platform target should inherit config.json from root"

    def test_child_no_content_cascade(
        self, sync_instance: VaultSync, vault_root: Path, mock_repo: Path
    ) -> None:
        """Platform target does NOT get root's skills content."""
        vault_repo = vault_root / "repos" / "TestRepo"
        targets = sync_instance.discover_sync_targets(vault_repo, mock_repo)
        plat = [t for t in targets if t.repo_path == mock_repo / "platform"][0]

        manifest = sync_instance.build_target_manifest(
            "copilot", plat, vault_repo, mock_repo
        )

        # Root has skills/changelog-generator/SKILL.md — platform should NOT inherit it
        # Platform has its OWN skills/changelog-generator/SKILL.md from vault platform/skills/
        key = Path("skills/changelog-generator/SKILL.md")
        if key in manifest:
            source = str(manifest[key])
            assert "platform" in source, \
                f"Platform's changelog-generator should come from platform vault, got {source}"

    def test_vault_manifest_copilot(
        self, sync_instance: VaultSync, vault_root: Path
    ) -> None:
        manifest = sync_instance.build_vault_manifest("copilot")

        assert Path("skills/git-workflow.md") in manifest
        assert Path("skills/testing.md") in manifest
        assert Path("skills/setup.md") in manifest
        source = manifest[Path("skills/setup.md")][0]
        assert "setup.copilot.md" in str(source)
        assert Path("skills/python.md") in manifest
        assert Path("skills/api-design.md") in manifest

    def test_vault_manifest_claude(
        self, sync_instance: VaultSync, vault_root: Path
    ) -> None:
        manifest = sync_instance.build_vault_manifest("claude")

        assert Path("skills/database.md") in manifest
        assert Path("skills/python.md") in manifest
        assert Path("skills/api-design.md") not in manifest, \
            "Claude should not have api-design.md (copilot-only)"
