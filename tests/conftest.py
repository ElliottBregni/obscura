# pyright: ignore-all
"""Shared pytest fixtures for FV-Copilot test suite.

All fixtures use tmp_path so tests are fully CI-safe — no real filesystem
dependencies, no mutation of ~/.github or ~/git/.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sync import VaultSync


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk(base: Path, rel_path: str, content: str = "") -> Path:
    """Create a file at *base / rel_path*, creating parent dirs as needed."""
    fp = base / rel_path
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content)
    return fp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def vault_root(tmp_path: Path) -> Path:
    """Miniature vault under tmp_path/vault/ mirroring real vault structure."""
    vault = tmp_path / "vault"
    vault.mkdir()

    # --- agents/INDEX.md ---
    _mk(vault, "agents/INDEX.md", (
        "# Agent Registry\n"
        "\n"
        "## Active Agents\n"
        "- copilot\n"
        "- claude\n"
    ))

    # --- repos/TestRepo/ (in-repo vault content) ---
    _mk(vault, "repos/TestRepo/agent.md", "# Agent config\n")
    _mk(vault, "repos/TestRepo/config.json", "{}\n")
    _mk(vault, "repos/TestRepo/copilot-instructions.md", "# Copilot instructions\n")

    # Root-level skills
    _mk(vault, "repos/TestRepo/skills/subagent/Models/Skill.md", "# Universal skill\n")
    _mk(vault, "repos/TestRepo/skills/subagent/Models/skill.copilot.md", "# Copilot skill\n")
    _mk(vault, "repos/TestRepo/skills/changelog-generator/SKILL.md", "# Changelog\n")

    # platform/ — matches repo's platform/ dir for recursive discovery
    _mk(vault, "repos/TestRepo/platform/skills/subagent/Models/Skill.md", "# Platform skill\n")
    _mk(vault, "repos/TestRepo/platform/skills/changelog-generator/SKILL.md", "# Platform changelog\n")

    # platform/skills/partview_core/ — matches repo's platform/partview_core/
    _mk(vault, "repos/TestRepo/platform/skills/partview_core/skills/pv-skill.md", "# PV skill\n")

    # --- repos/INDEX.md (will be updated by sync_instance to point to mock_repo) ---
    # Placeholder — sync_instance fixture overwrites with correct absolute path
    _mk(vault, "repos/INDEX.md", "# placeholder\n")

    # --- Vault-wide content dirs ---
    # skills/
    _mk(vault, "skills/git-workflow.md", "# Git workflow\n")
    _mk(vault, "skills/testing.md", "# Testing\n")
    _mk(vault, "skills/setup.md", "# Universal setup\n")
    _mk(vault, "skills/setup.copilot.md", "# Copilot setup override\n")
    _mk(vault, "skills/api-design.copilot.md", "# API design (copilot only)\n")
    _mk(vault, "skills/python.md", "# Python (universal)\n")
    _mk(vault, "skills/skills.copilot/python.md", "# Copilot python agent-dir\n")
    _mk(vault, "skills/skills.claude/database.md", "# Claude database agent-dir\n")

    # instructions/
    _mk(vault, "instructions/general.md", "# General instructions\n")

    # docs/
    _mk(vault, "docs/AUTO-SYNC.md", "# Auto sync docs\n")

    return vault


@pytest.fixture()
def mock_repo(tmp_path: Path) -> Path:
    """Fake code repo under tmp_path/TestRepo/ with the directory structure
    needed to trigger recursive discovery (platform/, platform/partview_core/).

    Name MUST match the vault repo dir (repos/TestRepo/) so sync_all()
    can pair them: repo_path.name == vault_repo_dir.name.
    """
    repo = tmp_path / "TestRepo"
    (repo / "platform" / "partview_core").mkdir(parents=True)
    return repo


@pytest.fixture()
def sync_instance(vault_root: Path, mock_repo: Path) -> VaultSync:
    """VaultSync pointed at the fixture vault, with repos/INDEX.md
    containing the absolute path to mock_repo.
    """
    index = vault_root / "repos" / "INDEX.md"
    index.write_text(f"{mock_repo}\n")
    return VaultSync(vault_path=vault_root)


@pytest.fixture()
def mock_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake home directory. Monkeypatches Path.home() and os.path.expanduser
    so sync_system() writes to tmp instead of the real ~/ .
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    _original_expanduser = os.path.expanduser

    def _fake_expanduser(path: str) -> str:
        if path.startswith("~"):
            return str(home) + path[1:]
        return _original_expanduser(path)

    monkeypatch.setattr(os.path, "expanduser", _fake_expanduser)
    return home


@pytest.fixture()
def mock_lock_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect LOCK_FILE to tmp_path so watcher tests don't touch /tmp/."""
    lock = tmp_path / "test-watcher.pid"
    monkeypatch.setattr("sync.LOCK_FILE", lock)
    return lock


# ---------------------------------------------------------------------------
# Variant / profile fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def variant_vault(tmp_path: Path) -> Path:
    """Vault with model-variant and role files for VariantSelector tests."""
    vault = tmp_path / "variant_vault"
    vault.mkdir()

    _mk(vault, "agents/INDEX.md", (
        "# Agent Registry\n\n## Active Agents\n- copilot\n- claude\n"
    ))
    _mk(vault, "repos/INDEX.md", "# placeholder\n")

    # Vault-wide skills with model variants
    _mk(vault, "skills/setup.md", "# Base setup\n")
    _mk(vault, "skills/setup.opus.md", "# Opus setup\n")
    _mk(vault, "skills/setup.sonnet.md", "# Sonnet setup\n")
    _mk(vault, "skills/git-workflow.md", "# Git workflow (no variants)\n")
    _mk(vault, "skills/testing.md", "# Testing (no variants)\n")

    # Agent-specific + model variant
    _mk(vault, "skills/config.copilot.md", "# Copilot config base\n")
    _mk(vault, "skills/config.copilot.opus.md", "# Copilot config opus\n")

    # Role files
    _mk(vault, "skills/roles/reviewer.md", "# Reviewer role\n")
    _mk(vault, "skills/roles/implementer.md", "# Implementer role\n")
    _mk(vault, "skills/roles/architect/overview.md", "# Architect overview\n")
    _mk(vault, "skills/roles/architect/patterns.md", "# Architect patterns\n")

    # instructions/
    _mk(vault, "instructions/general.md", "# General\n")
    _mk(vault, "instructions/general.opus.md", "# General opus\n")

    # docs/ (no variants — tests that non-variant files pass through)
    _mk(vault, "docs/AUTO-SYNC.md", "# Auto sync\n")

    return vault


@pytest.fixture()
def variant_sync(variant_vault: Path) -> VaultSync:
    """VaultSync with no profile set (baseline — no variant filtering)."""
    return VaultSync(vault_path=variant_vault)
