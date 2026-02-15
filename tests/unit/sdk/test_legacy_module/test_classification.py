"""Tests for VaultSync.classify_file() — agent-specific file classification."""

from __future__ import annotations

from pathlib import Path

from scripts.sync import VaultSync


class TestFileClassification:
    """File classification: UNIVERSAL, AGENT_NAMED, AGENT_NESTED, AGENT_DIR, SKIP."""

    def test_classify_universal_file(
        self, sync_instance: VaultSync, vault_root: Path
    ) -> None:
        vault_repo = vault_root / "repos" / "TestRepo"
        cls, dest = sync_instance.classify_file(
            vault_repo / "agent.md", vault_repo, "copilot"
        )
        assert cls == "UNIVERSAL", f"Expected UNIVERSAL, got {cls}"
        assert dest == Path("agent.md"), f"Expected agent.md, got {dest}"

    def test_classify_agent_named_file_match(
        self, sync_instance: VaultSync, vault_root: Path
    ) -> None:
        vault_repo = vault_root / "repos" / "TestRepo"
        cls, dest = sync_instance.classify_file(
            vault_repo / "copilot-instructions.md", vault_repo, "copilot"
        )
        assert cls == "AGENT_NAMED", f"Expected AGENT_NAMED, got {cls}"
        assert dest == Path("copilot-instructions.md")

    def test_classify_agent_named_file_skip(
        self, sync_instance: VaultSync, vault_root: Path
    ) -> None:
        vault_repo = vault_root / "repos" / "TestRepo"
        cls, _ = sync_instance.classify_file(
            vault_repo / "copilot-instructions.md", vault_repo, "claude"
        )
        assert cls == "SKIP", f"Claude should skip copilot-instructions.md, got {cls}"

    def test_classify_nested_override_match(
        self, sync_instance: VaultSync, vault_root: Path
    ) -> None:
        vault_repo = vault_root / "repos" / "TestRepo"
        fp = vault_repo / "skills" / "subagent" / "Models" / "skill.copilot.md"
        cls, dest = sync_instance.classify_file(fp, vault_repo, "copilot")
        assert cls == "AGENT_NESTED", f"Expected AGENT_NESTED, got {cls}"
        assert dest.name == "skill.md", f"Expected skill.md, got {dest.name}"

    def test_classify_nested_override_skip(
        self, sync_instance: VaultSync, vault_root: Path
    ) -> None:
        vault_repo = vault_root / "repos" / "TestRepo"
        fp = vault_repo / "skills" / "subagent" / "Models" / "skill.copilot.md"
        cls, _ = sync_instance.classify_file(fp, vault_repo, "claude")
        assert cls == "SKIP", f"Claude should skip skill.copilot.md, got {cls}"

    def test_classify_excluded_file(
        self, sync_instance: VaultSync, vault_root: Path
    ) -> None:
        vault_repo = vault_root / "repos" / "TestRepo"
        cls, _ = sync_instance.classify_file(
            vault_repo / "command-history-state.json", vault_repo, "copilot"
        )
        assert cls == "SKIP", f"Expected SKIP for excluded file, got {cls}"

    def test_classify_vault_nested_override(
        self, sync_instance: VaultSync, vault_root: Path
    ) -> None:
        """Vault-level nested override: skills/setup.copilot.md -> skills/setup.md"""
        fp = vault_root / "skills" / "setup.copilot.md"
        cls, dest = sync_instance.classify_file(fp, vault_root, "copilot")
        assert cls == "AGENT_NESTED", f"Expected AGENT_NESTED, got {cls}"
        assert dest == Path("skills/setup.md"), f"Expected skills/setup.md, got {dest}"

    def test_classify_vault_agent_dir(
        self, sync_instance: VaultSync, vault_root: Path
    ) -> None:
        """Vault-level agent dir: skills/skills.copilot/python.md -> skills/python.md"""
        fp = vault_root / "skills" / "skills.copilot" / "python.md"
        cls, dest = sync_instance.classify_file(fp, vault_root, "copilot")
        assert cls == "AGENT_DIR", f"Expected AGENT_DIR, got {cls}"
        assert dest == Path("skills/python.md"), f"Expected skills/python.md, got {dest}"

    def test_classify_vault_agent_dir_different_name(
        self, sync_instance: VaultSync, vault_root: Path
    ) -> None:
        """Agent subdir: instructions/setup.copilot/x.md -> instructions/setup/x.md"""
        # Create the file in the fixture vault
        fp = vault_root / "instructions" / "setup.copilot" / "x.md"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("# test\n")

        cls, dest = sync_instance.classify_file(fp, vault_root, "copilot")
        assert cls == "AGENT_DIR", f"Expected AGENT_DIR, got {cls}"
        assert dest == Path("instructions/setup/x.md"), \
            f"Expected instructions/setup/x.md, got {dest}"

    def test_classify_vault_agent_dir_skip(
        self, sync_instance: VaultSync, vault_root: Path
    ) -> None:
        """Agent dir for other agent should be skipped."""
        fp = vault_root / "skills" / "skills.claude" / "database.md"
        cls, _ = sync_instance.classify_file(fp, vault_root, "copilot")
        assert cls == "SKIP", f"Copilot should skip skills.claude/ files, got {cls}"
