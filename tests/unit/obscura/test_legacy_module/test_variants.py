"""Tests for VariantSelector and SyncProfile — model/role variant filtering."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import pytest

from scripts.sync import (
    VariantSelector,
    VaultSync,
    parse_sync_profile,
    SYNC_PROFILE_FILE,
)


# ---------------------------------------------------------------------------
# SyncProfile parsing
# ---------------------------------------------------------------------------


class TestSyncProfileParsing:
    """Parse .sync-profile.yml files."""

    def test_parse_full_profile(self, tmp_path: Path) -> None:
        f = tmp_path / SYNC_PROFILE_FILE
        f.write_text("model: opus\nrole: reviewer\n")
        profile = parse_sync_profile(f)
        assert profile.model == "opus"
        assert profile.role == "reviewer"

    def test_parse_model_only(self, tmp_path: Path) -> None:
        f = tmp_path / SYNC_PROFILE_FILE
        f.write_text("model: sonnet\n")
        profile = parse_sync_profile(f)
        assert profile.model == "sonnet"
        assert profile.role is None

    def test_parse_role_only(self, tmp_path: Path) -> None:
        f = tmp_path / SYNC_PROFILE_FILE
        f.write_text("role: implementer\n")
        profile = parse_sync_profile(f)
        assert profile.model is None
        assert profile.role == "implementer"

    def test_parse_missing_file(self, tmp_path: Path) -> None:
        profile = parse_sync_profile(tmp_path / "nonexistent.yml")
        assert profile.model is None
        assert profile.role is None

    def test_parse_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / SYNC_PROFILE_FILE
        f.write_text("")
        profile = parse_sync_profile(f)
        assert profile.model is None
        assert profile.role is None

    def test_parse_comments_ignored(self, tmp_path: Path) -> None:
        f = tmp_path / SYNC_PROFILE_FILE
        f.write_text("# This is a comment\nmodel: haiku\n# Another comment\n")
        profile = parse_sync_profile(f)
        assert profile.model == "haiku"
        assert profile.role is None


# ---------------------------------------------------------------------------
# VariantSelector — model swaps
# ---------------------------------------------------------------------------


class TestModelVariantSwaps:
    """VariantSelector model swap logic."""

    def test_no_profile_strips_model_variants(self) -> None:
        """With no model set, model-variant files are stripped, base remains."""
        selector = VariantSelector(model=None, role=None)
        manifest = {
            Path("skills/setup.md"): Path("/vault/skills/setup.md"),
            Path("skills/setup.opus.md"): Path("/vault/skills/setup.opus.md"),
            Path("skills/setup.sonnet.md"): Path("/vault/skills/setup.sonnet.md"),
            Path("skills/testing.md"): Path("/vault/skills/testing.md"),
        }
        result = selector.select(manifest)
        assert Path("skills/setup.md") in result
        assert Path("skills/setup.opus.md") not in result
        assert Path("skills/setup.sonnet.md") not in result
        assert Path("skills/testing.md") in result

    def test_model_opus_swaps_base(self) -> None:
        """With model=opus, opus variant replaces base at same dest path."""
        selector = VariantSelector(model="opus", role=None)
        manifest = {
            Path("skills/setup.md"): Path("/vault/skills/setup.md"),
            Path("skills/setup.opus.md"): Path("/vault/skills/setup.opus.md"),
            Path("skills/setup.sonnet.md"): Path("/vault/skills/setup.sonnet.md"),
        }
        result = selector.select(manifest)
        # Opus variant replaces base
        assert result[Path("skills/setup.md")] == Path("/vault/skills/setup.opus.md")
        # Sonnet variant stripped
        assert Path("skills/setup.sonnet.md") not in result
        # Original opus dest path gone (merged into base)
        assert Path("skills/setup.opus.md") not in result

    def test_model_sonnet_swaps_base(self) -> None:
        """With model=sonnet, sonnet variant replaces base."""
        selector = VariantSelector(model="sonnet", role=None)
        manifest = {
            Path("skills/setup.md"): Path("/vault/skills/setup.md"),
            Path("skills/setup.opus.md"): Path("/vault/skills/setup.opus.md"),
            Path("skills/setup.sonnet.md"): Path("/vault/skills/setup.sonnet.md"),
        }
        result = selector.select(manifest)
        assert result[Path("skills/setup.md")] == Path("/vault/skills/setup.sonnet.md")
        assert Path("skills/setup.opus.md") not in result

    def test_model_variant_no_base_file(self) -> None:
        """Model variant without a base file: included when model matches."""
        selector = VariantSelector(model="opus", role=None)
        manifest = {
            Path("skills/deep-analysis.opus.md"): Path(
                "/vault/skills/deep-analysis.opus.md"
            ),
        }
        result = selector.select(manifest)
        # Should land at base dest (without model segment)
        assert Path("skills/deep-analysis.md") in result
        assert result[Path("skills/deep-analysis.md")] == Path(
            "/vault/skills/deep-analysis.opus.md"
        )

    def test_model_variant_no_base_wrong_model(self) -> None:
        """Model variant without base: excluded when model doesn't match."""
        selector = VariantSelector(model="sonnet", role=None)
        manifest = {
            Path("skills/deep-analysis.opus.md"): Path(
                "/vault/skills/deep-analysis.opus.md"
            ),
        }
        result = selector.select(manifest)
        assert len(result) == 0

    def test_non_variant_files_pass_through(self) -> None:
        """Files without model patterns pass through unchanged."""
        selector = VariantSelector(model="opus", role=None)
        manifest = {
            Path("skills/git-workflow.md"): Path("/vault/skills/git-workflow.md"),
            Path("skills/testing.md"): Path("/vault/skills/testing.md"),
            Path("docs/AUTO-SYNC.md"): Path("/vault/docs/AUTO-SYNC.md"),
        }
        result = selector.select(manifest)
        assert result == manifest


# ---------------------------------------------------------------------------
# VariantSelector — role filtering
# ---------------------------------------------------------------------------


class TestRoleFiltering:
    """VariantSelector role filter logic."""

    def test_no_role_strips_all_role_files(self) -> None:
        """With no role set, all files under roles/ are stripped."""
        selector = VariantSelector(model=None, role=None)
        manifest = {
            Path("skills/git-workflow.md"): Path("/vault/skills/git-workflow.md"),
            Path("skills/roles/reviewer.md"): Path("/vault/skills/roles/reviewer.md"),
            Path("skills/roles/implementer.md"): Path(
                "/vault/skills/roles/implementer.md"
            ),
        }
        result = selector.select(manifest)
        assert Path("skills/git-workflow.md") in result
        assert Path("skills/roles/reviewer.md") not in result
        assert Path("skills/roles/implementer.md") not in result

    def test_role_reviewer_includes_matching(self) -> None:
        """With role=reviewer, only reviewer role files are included."""
        selector = VariantSelector(model=None, role="reviewer")
        manifest = {
            Path("skills/git-workflow.md"): Path("/vault/skills/git-workflow.md"),
            Path("skills/roles/reviewer.md"): Path("/vault/skills/roles/reviewer.md"),
            Path("skills/roles/implementer.md"): Path(
                "/vault/skills/roles/implementer.md"
            ),
        }
        result = selector.select(manifest)
        assert Path("skills/git-workflow.md") in result
        assert Path("skills/roles/reviewer.md") in result
        assert Path("skills/roles/implementer.md") not in result

    def test_role_dir_match(self) -> None:
        """Role as directory: roles/architect/overview.md matches role=architect."""
        selector = VariantSelector(model=None, role="architect")
        manifest = {
            Path("skills/roles/architect/overview.md"): Path("/v/overview.md"),
            Path("skills/roles/architect/patterns.md"): Path("/v/patterns.md"),
            Path("skills/roles/reviewer.md"): Path("/v/reviewer.md"),
        }
        result = selector.select(manifest)
        assert Path("skills/roles/architect/overview.md") in result
        assert Path("skills/roles/architect/patterns.md") in result
        assert Path("skills/roles/reviewer.md") not in result

    def test_role_case_insensitive(self) -> None:
        """Role matching is case-insensitive."""
        selector = VariantSelector(model=None, role="Reviewer")
        manifest = {
            Path("skills/roles/reviewer.md"): Path("/vault/roles/reviewer.md"),
        }
        result = selector.select(manifest)
        assert Path("skills/roles/reviewer.md") in result


# ---------------------------------------------------------------------------
# VariantSelector — combined model + role
# ---------------------------------------------------------------------------


class TestCombinedModelRole:
    """Both model and role active simultaneously."""

    def test_model_and_role_together(self) -> None:
        selector = VariantSelector(model="opus", role="reviewer")
        manifest = {
            Path("skills/setup.md"): Path("/v/setup.md"),
            Path("skills/setup.opus.md"): Path("/v/setup.opus.md"),
            Path("skills/setup.sonnet.md"): Path("/v/setup.sonnet.md"),
            Path("skills/roles/reviewer.md"): Path("/v/reviewer.md"),
            Path("skills/roles/implementer.md"): Path("/v/implementer.md"),
            Path("skills/testing.md"): Path("/v/testing.md"),
        }
        result = selector.select(manifest)
        # Model swap: opus replaces base
        assert result[Path("skills/setup.md")] == Path("/v/setup.opus.md")
        assert Path("skills/setup.sonnet.md") not in result
        # Role filter: only reviewer
        assert Path("skills/roles/reviewer.md") in result
        assert Path("skills/roles/implementer.md") not in result
        # Regular files pass through
        assert Path("skills/testing.md") in result

    def test_empty_manifest(self) -> None:
        """Empty manifest returns empty."""
        selector = VariantSelector(model="opus", role="reviewer")
        assert selector.select({}) == {}


# ---------------------------------------------------------------------------
# VaultSync profile loading
# ---------------------------------------------------------------------------


class TestVaultSyncProfileLoading:
    """Profile loading and per-repo override merging."""

    def test_no_profile_file_defaults(self, variant_vault: Path) -> None:
        """VaultSync with no .sync-profile.yml uses empty profile."""
        vs = VaultSync(vault_path=variant_vault)
        assert vs.global_profile.model is None
        assert vs.global_profile.role is None

    def test_global_profile_loaded(self, variant_vault: Path) -> None:
        """VaultSync reads .sync-profile.yml from vault root."""
        (variant_vault / SYNC_PROFILE_FILE).write_text("model: opus\nrole: reviewer\n")
        vs = VaultSync(vault_path=variant_vault)
        assert vs.global_profile.model == "opus"
        assert vs.global_profile.role == "reviewer"
        assert vs.selector.model == "opus"
        assert vs.selector.role == "reviewer"

    def test_per_repo_profile_overrides(self, variant_vault: Path) -> None:
        """Per-repo profile overrides global values."""
        (variant_vault / SYNC_PROFILE_FILE).write_text("model: opus\nrole: reviewer\n")
        repo_dir = variant_vault / "repos" / "TestRepo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        (repo_dir / SYNC_PROFILE_FILE).write_text("model: sonnet\n")

        vs = VaultSync(vault_path=variant_vault)
        repo_sel = vs.get_selector("TestRepo")
        assert repo_sel.model == "sonnet"
        # Role inherits from global
        assert repo_sel.role == "reviewer"

    def test_per_repo_no_override_uses_global(self, variant_vault: Path) -> None:
        """No per-repo profile → uses global selector."""
        (variant_vault / SYNC_PROFILE_FILE).write_text("model: haiku\n")
        vs = VaultSync(vault_path=variant_vault)
        sel = vs.get_selector("NonExistentRepo")
        assert sel.model == "haiku"


# ---------------------------------------------------------------------------
# Integration: VariantSelector with real sync pipeline
# ---------------------------------------------------------------------------


class TestVariantIntegration:
    """End-to-end variant filtering through the sync pipeline."""

    def test_system_sync_with_model_opus(
        self,
        variant_vault: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """System sync with model=opus swaps base files for opus variants."""
        home = tmp_path / "home"
        home.mkdir()

        def _home() -> Path:
            return home

        monkeypatch.setattr(Path, "home", staticmethod(_home))
        _orig: Callable[[str], str] = os.path.expanduser

        def _expanduser(p: str) -> str:
            return str(home) + p[1:] if p.startswith("~") else _orig(p)

        monkeypatch.setattr(os.path, "expanduser", _expanduser)

        (variant_vault / SYNC_PROFILE_FILE).write_text("model: opus\n")
        vs = VaultSync(vault_path=variant_vault)
        vs.sync_system(agent="copilot")

        github_skills = home / ".github" / "skills"
        # setup.md should exist (opus variant swapped in)
        assert (github_skills / "setup.md").is_symlink()
        # It should point to the opus source
        target = (github_skills / "setup.md").resolve()
        assert "setup.opus.md" in target.name
        # Sonnet variant should NOT exist
        assert not (github_skills / "setup.sonnet.md").exists()
        assert not (github_skills / "setup.opus.md").exists()

    def test_system_sync_no_profile_keeps_base(
        self,
        variant_vault: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """System sync with no profile keeps base files, strips model variants."""
        home = tmp_path / "home"
        home.mkdir()

        def _home() -> Path:
            return home

        monkeypatch.setattr(Path, "home", staticmethod(_home))
        _orig: Callable[[str], str] = os.path.expanduser

        def _expanduser(p: str) -> str:
            return str(home) + p[1:] if p.startswith("~") else _orig(p)

        monkeypatch.setattr(os.path, "expanduser", _expanduser)

        # No .sync-profile.yml
        vs = VaultSync(vault_path=variant_vault)
        vs.sync_system(agent="copilot")

        github_skills = home / ".github" / "skills"
        # Base setup.md should exist pointing to base source
        assert (github_skills / "setup.md").is_symlink()
        target = (github_skills / "setup.md").resolve()
        assert target.name == "setup.md"
        # Model variants should NOT be synced
        assert not (github_skills / "setup.opus.md").exists()
        assert not (github_skills / "setup.sonnet.md").exists()

    def test_system_sync_with_role(
        self,
        variant_vault: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """System sync with role=reviewer includes only reviewer role files."""
        home = tmp_path / "home"
        home.mkdir()

        def _home() -> Path:
            return home

        monkeypatch.setattr(Path, "home", staticmethod(_home))
        _orig: Callable[[str], str] = os.path.expanduser

        def _expanduser(p: str) -> str:
            return str(home) + p[1:] if p.startswith("~") else _orig(p)

        monkeypatch.setattr(os.path, "expanduser", _expanduser)

        (variant_vault / SYNC_PROFILE_FILE).write_text("role: reviewer\n")
        vs = VaultSync(vault_path=variant_vault)
        vs.sync_system(agent="copilot")

        github_skills = home / ".github" / "skills"
        # Reviewer role file should be synced
        assert (github_skills / "roles" / "reviewer.md").is_symlink()
        # Implementer should NOT
        assert not (github_skills / "roles" / "implementer.md").exists()
