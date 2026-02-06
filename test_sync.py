#!/usr/bin/env python3
"""Tests for FV-Copilot vault sync.

Run:  python3 test_sync.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

# Add vault root to path so we can import sync
sys.path.insert(0, str(Path(__file__).parent))
from sync import VaultSync, VAULT_PATH, REPOS_BASE, PRIORITY

TEST_REPO = Path.home() / "git" / "FV-Platform-Main"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

passed = 0
failed = 0


def run_test(name: str, func):
    global passed, failed
    try:
        func()
        print(f"  PASS  {name}")
        passed += 1
    except AssertionError as e:
        print(f"  FAIL  {name}: {e}")
        failed += 1
    except Exception as e:
        print(f"  ERR   {name}: {type(e).__name__}: {e}")
        failed += 1


def cleanup_target():
    """Remove .github and .claude from target repo if they exist."""
    for target in [".github", ".claude"]:
        t = TEST_REPO / target
        if t.is_symlink():
            t.unlink()
        elif t.is_dir():
            shutil.rmtree(t)


# ---------------------------------------------------------------------------
# Unit tests: Config parsing
# ---------------------------------------------------------------------------

def test_agent_path_mapping():
    vs = VaultSync(VAULT_PATH)
    assert vs.get_agent_target("copilot") == ".github", "copilot should map to .github"
    assert vs.get_agent_target("claude") == ".claude", "claude should map to .claude"
    assert vs.get_agent_target("cursor") == ".cursor", "cursor should map to .cursor"
    assert vs.get_agent_target("custom") == ".custom", "unknown should map to .custom"


def test_registered_agents():
    vs = VaultSync(VAULT_PATH)
    agents = vs.get_registered_agents()
    assert "copilot" in agents, f"copilot not in {agents}"
    assert "claude" in agents, f"claude not in {agents}"


def test_managed_repos():
    vs = VaultSync(VAULT_PATH)
    repos = vs.get_managed_repos()
    names = [r.name for r in repos]
    assert "FV-Platform-Main" in names, f"FV-Platform-Main not in {names}"


# ---------------------------------------------------------------------------
# Unit tests: File classification
# ---------------------------------------------------------------------------

def test_classify_universal_file():
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    cls, dest = vs.classify_file(vault_repo / "agent.md", vault_repo, "copilot")
    assert cls == "UNIVERSAL", f"Expected UNIVERSAL, got {cls}"
    assert dest == Path("agent.md"), f"Expected agent.md, got {dest}"


def test_classify_agent_named_file_match():
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    cls, dest = vs.classify_file(
        vault_repo / "copilot-instructions.md", vault_repo, "copilot"
    )
    assert cls == "AGENT_NAMED", f"Expected AGENT_NAMED, got {cls}"
    assert dest == Path("copilot-instructions.md")


def test_classify_agent_named_file_skip():
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    cls, _ = vs.classify_file(
        vault_repo / "copilot-instructions.md", vault_repo, "claude"
    )
    assert cls == "SKIP", f"Claude should skip copilot-instructions.md, got {cls}"


def test_classify_nested_override_match():
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    fp = vault_repo / "skills" / "subagent" / "Models" / "skill.copilot.md"
    cls, dest = vs.classify_file(fp, vault_repo, "copilot")
    assert cls == "AGENT_NESTED", f"Expected AGENT_NESTED, got {cls}"
    assert dest.name == "skill.md", f"Expected skill.md, got {dest.name}"


def test_classify_nested_override_skip():
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    fp = vault_repo / "skills" / "subagent" / "Models" / "skill.copilot.md"
    cls, _ = vs.classify_file(fp, vault_repo, "claude")
    assert cls == "SKIP", f"Claude should skip skill.copilot.md, got {cls}"


def test_classify_excluded_file():
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    cls, _ = vs.classify_file(
        vault_repo / "command-history-state.json", vault_repo, "copilot"
    )
    assert cls == "SKIP", f"Expected SKIP for excluded file, got {cls}"


def test_classify_vault_nested_override():
    """Vault-level nested override: skills/setup.copilot.md -> skills/setup.md"""
    vs = VaultSync(VAULT_PATH)
    fp = VAULT_PATH / "skills" / "setup.copilot.md"
    cls, dest = vs.classify_file(fp, VAULT_PATH, "copilot")
    assert cls == "AGENT_NESTED", f"Expected AGENT_NESTED, got {cls}"
    assert dest == Path("skills/setup.md"), f"Expected skills/setup.md, got {dest}"


def test_classify_vault_agent_dir():
    """Vault-level agent dir: skills/skills.copilot/python.md -> skills/python.md"""
    vs = VaultSync(VAULT_PATH)
    fp = VAULT_PATH / "skills" / "skills.copilot" / "python.md"
    cls, dest = vs.classify_file(fp, VAULT_PATH, "copilot")
    assert cls == "AGENT_DIR", f"Expected AGENT_DIR, got {cls}"
    assert dest == Path("skills/python.md"), f"Expected skills/python.md, got {dest}"


def test_classify_vault_agent_dir_different_name():
    """Agent subdir: instructions/setup.copilot/x.md -> instructions/setup/x.md"""
    vs = VaultSync(VAULT_PATH)
    fp = VAULT_PATH / "instructions" / "setup.copilot" / "x.md"
    if fp.exists():
        cls, dest = vs.classify_file(fp, VAULT_PATH, "copilot")
        assert cls == "AGENT_DIR", f"Expected AGENT_DIR, got {cls}"
        assert dest == Path("instructions/setup/x.md"), f"Expected instructions/setup/x.md, got {dest}"


def test_classify_vault_agent_dir_skip():
    """Agent dir for other agent should be skipped."""
    vs = VaultSync(VAULT_PATH)
    fp = VAULT_PATH / "skills" / "skills.claude" / "database.md"
    cls, _ = vs.classify_file(fp, VAULT_PATH, "copilot")
    assert cls == "SKIP", f"Copilot should skip skills.claude/ files, got {cls}"


# ---------------------------------------------------------------------------
# Unit tests: Manifest building
# ---------------------------------------------------------------------------

def test_repo_manifest_copilot():
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    manifest = vs.build_repo_manifest("copilot", vault_repo)

    # Copilot should get copilot-instructions.md
    assert Path("copilot-instructions.md") in manifest, \
        "copilot-instructions.md missing from copilot manifest"

    # Copilot should get agent.md (universal)
    assert Path("agent.md") in manifest, "agent.md missing from copilot manifest"

    # Nested override should be stripped
    assert Path("skills/subagent/Models/skill.md") in manifest, \
        "skill.md (stripped from skill.copilot.md) missing"
    source = manifest[Path("skills/subagent/Models/skill.md")][0]
    assert "skill.copilot.md" in str(source), \
        f"skill.md should point to skill.copilot.md, got {source}"


def test_repo_manifest_claude():
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    manifest = vs.build_repo_manifest("claude", vault_repo)

    # Claude should NOT get copilot-instructions.md
    assert Path("copilot-instructions.md") not in manifest, \
        "Claude should not have copilot-instructions.md"

    # Claude should get agent.md (universal)
    assert Path("agent.md") in manifest, "agent.md missing from claude manifest"

    # Claude gets its own nested override
    assert Path("skills/subagent/Models/skill.md") in manifest, \
        "skill.md (stripped from skill.claude.md) missing"


def test_vault_manifest_copilot():
    vs = VaultSync(VAULT_PATH)
    manifest = vs.build_vault_manifest("copilot")

    # Universal skills
    assert Path("skills/git-workflow.md") in manifest, \
        "git-workflow.md missing from vault manifest"
    assert Path("skills/testing.md") in manifest, \
        "testing.md missing from vault manifest"

    # Nested override wins: setup.copilot.md -> skills/setup.md
    assert Path("skills/setup.md") in manifest, \
        "skills/setup.md missing from vault manifest"
    source = manifest[Path("skills/setup.md")][0]
    assert "setup.copilot.md" in str(source), \
        f"setup.md should point to setup.copilot.md, got {source}"

    # Agent dir content: skills.copilot/python.md -> skills/python.md
    assert Path("skills/python.md") in manifest, \
        "skills/python.md (from skills.copilot/) missing"
    assert Path("skills/api-design.md") in manifest, \
        "skills/api-design.md (from skills.copilot/) missing"


def test_vault_manifest_claude():
    vs = VaultSync(VAULT_PATH)
    manifest = vs.build_vault_manifest("claude")

    # Claude-specific agent dir content
    assert Path("skills/database.md") in manifest, \
        "skills/database.md (from skills.claude/) missing"
    assert Path("skills/python.md") in manifest, \
        "skills/python.md (from skills.claude/) missing"

    # Claude should NOT see copilot agent dir content
    assert Path("skills/api-design.md") not in manifest, \
        "Claude should not have api-design.md (copilot-only)"


def test_full_manifest_merge():
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    manifest = vs.build_full_manifest("copilot", vault_repo)

    # Should have repo content
    assert Path("copilot-instructions.md") in manifest
    assert Path("agent.md") in manifest

    # Should have vault content
    assert Path("skills/git-workflow.md") in manifest

    # Repo skills should coexist with vault skills
    assert Path("skills/subagent/Skill.md") in manifest  # from repo
    assert Path("skills/testing.md") in manifest  # from vault


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

def test_sync_creates_real_dirs():
    """Full integration: sync creates real directories with per-file symlinks."""
    cleanup_target()

    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"

    vs.sync_repo("copilot", TEST_REPO, vault_repo)
    vs.sync_repo("claude", TEST_REPO, vault_repo)

    # Real directories, not symlinks
    github = TEST_REPO / ".github"
    claude_dir = TEST_REPO / ".claude"
    assert github.is_dir() and not github.is_symlink(), \
        ".github should be a real directory"
    assert claude_dir.is_dir() and not claude_dir.is_symlink(), \
        ".claude should be a real directory"


def test_sync_agent_filtering():
    """Copilot sees copilot-instructions.md, Claude does not."""
    cleanup_target()

    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    vs.sync_repo("copilot", TEST_REPO, vault_repo)
    vs.sync_repo("claude", TEST_REPO, vault_repo)

    assert (TEST_REPO / ".github" / "copilot-instructions.md").exists(), \
        ".github should have copilot-instructions.md"
    assert not (TEST_REPO / ".claude" / "copilot-instructions.md").exists(), \
        ".claude should NOT have copilot-instructions.md"


def test_sync_universal_files():
    """Both agents see universal files."""
    assert (TEST_REPO / ".github" / "agent.md").exists(), \
        ".github should have agent.md"
    assert (TEST_REPO / ".claude" / "agent.md").exists(), \
        ".claude should have agent.md"


def test_sync_symlinks_point_to_vault():
    """Symlinks in target dir point back to vault."""
    link = TEST_REPO / ".github" / "agent.md"
    assert link.is_symlink(), "agent.md should be a symlink"
    target = link.resolve()
    assert "FV-Copilot" in str(target), \
        f"Symlink should point to vault, got {target}"


def test_sync_vault_content_present():
    """Vault-wide skills/instructions/docs are synced."""
    github = TEST_REPO / ".github"
    assert (github / "skills" / "git-workflow.md").exists(), \
        "Vault skill git-workflow.md should be in .github"
    assert (github / "skills" / "testing.md").exists(), \
        "Vault skill testing.md should be in .github"


def test_sync_nested_override():
    """Nested override: setup.copilot.md appears as setup.md for copilot."""
    link = TEST_REPO / ".github" / "skills" / "setup.md"
    assert link.exists(), "setup.md should exist in .github/skills/"
    assert link.is_symlink(), "setup.md should be a symlink"
    target = str(link.resolve())
    assert "setup.copilot.md" in target, \
        f"setup.md should point to setup.copilot.md, got {target}"


def test_sync_agent_dir_content():
    """Agent dir content: skills.copilot/python.md -> .github/skills/python.md"""
    link = TEST_REPO / ".github" / "skills" / "python.md"
    assert link.exists(), "python.md should exist in .github/skills/"
    target = str(link.resolve())
    assert "skills.copilot" in target, \
        f"python.md should point to skills.copilot/, got {target}"


def test_sync_idempotent():
    """Running sync twice produces same result."""
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"

    vs.sync_repo("copilot", TEST_REPO, vault_repo)
    count1 = sum(1 for _ in (TEST_REPO / ".github").rglob("*") if _.is_symlink())

    vs.sync_repo("copilot", TEST_REPO, vault_repo)
    count2 = sum(1 for _ in (TEST_REPO / ".github").rglob("*") if _.is_symlink())

    assert count1 == count2, f"Idempotent check: {count1} != {count2}"


def test_remove_links():
    """Remove links cleans up agent directories."""
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"

    # Ensure synced
    vs.sync_repo("copilot", TEST_REPO, vault_repo)
    assert (TEST_REPO / ".github").exists()

    # Remove
    vs.remove_links(TEST_REPO)

    assert not (TEST_REPO / ".github").exists(), ".github should be removed"
    assert not (TEST_REPO / ".claude").exists(), ".claude should be removed"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("FV-Copilot Sync Test Suite")
    print("=" * 40)

    print("\n--- Config Parsing ---")
    run_test("agent_path_mapping", test_agent_path_mapping)
    run_test("registered_agents", test_registered_agents)
    run_test("managed_repos", test_managed_repos)

    print("\n--- File Classification ---")
    run_test("classify_universal_file", test_classify_universal_file)
    run_test("classify_agent_named_match", test_classify_agent_named_file_match)
    run_test("classify_agent_named_skip", test_classify_agent_named_file_skip)
    run_test("classify_nested_override_match", test_classify_nested_override_match)
    run_test("classify_nested_override_skip", test_classify_nested_override_skip)
    run_test("classify_excluded_file", test_classify_excluded_file)
    run_test("classify_vault_nested_override", test_classify_vault_nested_override)
    run_test("classify_vault_agent_dir", test_classify_vault_agent_dir)
    run_test("classify_vault_agent_dir_different_name", test_classify_vault_agent_dir_different_name)
    run_test("classify_vault_agent_dir_skip", test_classify_vault_agent_dir_skip)

    print("\n--- Manifest Building ---")
    run_test("repo_manifest_copilot", test_repo_manifest_copilot)
    run_test("repo_manifest_claude", test_repo_manifest_claude)
    run_test("vault_manifest_copilot", test_vault_manifest_copilot)
    run_test("vault_manifest_claude", test_vault_manifest_claude)
    run_test("full_manifest_merge", test_full_manifest_merge)

    print("\n--- Integration: Sync ---")
    run_test("sync_creates_real_dirs", test_sync_creates_real_dirs)
    run_test("sync_agent_filtering", test_sync_agent_filtering)
    run_test("sync_universal_files", test_sync_universal_files)
    run_test("sync_symlinks_point_to_vault", test_sync_symlinks_point_to_vault)
    run_test("sync_vault_content_present", test_sync_vault_content_present)
    run_test("sync_nested_override", test_sync_nested_override)
    run_test("sync_agent_dir_content", test_sync_agent_dir_content)
    run_test("sync_idempotent", test_sync_idempotent)

    print("\n--- Integration: Cleanup ---")
    run_test("remove_links", test_remove_links)

    print(f"\n{'=' * 40}")
    print(f"Results: {passed} passed, {failed} failed")

    if failed:
        sys.exit(1)
    else:
        print("\nAll tests passed!")
        sys.exit(0)
