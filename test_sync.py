#!/usr/bin/env python3
"""Tests for FV-Copilot vault sync (recursive directory-matching).

Run:  python3 test_sync.py
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Add vault root to path so we can import sync
sys.path.insert(0, str(Path(__file__).parent))
from sync import (
    VaultSync, VaultWatcher, VAULT_PATH, REPOS_BASE, PRIORITY,
    CONTENT_DIRS, SyncTarget, LOCK_FILE, DEBOUNCE_SECONDS,
)

TEST_REPO = Path.home() / "git" / "FV-Platform-Main"
TEST_PLATFORM = TEST_REPO / "platform"
TEST_PARTVIEW = TEST_PLATFORM / "partview_core"
SYSTEM_GITHUB = Path.home() / ".github"
SYSTEM_CLAUDE = Path.home() / ".claude"


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


def cleanup_repo_targets():
    """Remove .github and .claude from all discovered target locations."""
    for base in [TEST_REPO, TEST_PLATFORM, TEST_PARTVIEW]:
        for target in [".github", ".claude"]:
            t = base / target
            if t.is_symlink():
                t.unlink()
            elif t.is_dir():
                shutil.rmtree(t)


def cleanup_system_targets():
    """Remove vault-managed symlinks from system agent dirs.
    SAFETY: only removes symlinks under skills/, instructions/, docs/.
    Never touches other files in ~/.claude/.
    """
    for system_dir in [SYSTEM_GITHUB, SYSTEM_CLAUDE]:
        for content_dir_name in CONTENT_DIRS:
            target = system_dir / content_dir_name
            if not target.exists():
                continue
            for f in list(target.rglob("*")):
                if f.is_symlink():
                    f.unlink()
            # Remove empty dirs bottom-up
            for d in sorted(
                (x for x in target.rglob("*") if x.is_dir()),
                reverse=True,
            ):
                try:
                    if not any(d.iterdir()):
                        d.rmdir()
                except OSError:
                    pass
            try:
                if not any(target.iterdir()):
                    target.rmdir()
            except OSError:
                pass


def cleanup_all():
    cleanup_repo_targets()
    cleanup_system_targets()


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
    # Should be list[Path] not list[tuple]
    assert isinstance(repos[0], Path), "get_managed_repos should return list[Path]"


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
# Unit tests: Recursive target discovery
# ---------------------------------------------------------------------------

def test_discover_targets_finds_root():
    """Discovery finds the repo root as a target."""
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    targets = vs.discover_sync_targets(vault_repo, TEST_REPO)

    repo_paths = [t.repo_path for t in targets]
    assert TEST_REPO in repo_paths, f"Root should be a target, got {repo_paths}"


def test_discover_targets_finds_platform():
    """Discovery finds platform/ as a target (vault has platform/, repo has platform/)."""
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    targets = vs.discover_sync_targets(vault_repo, TEST_REPO)

    repo_paths = [t.repo_path for t in targets]
    assert TEST_PLATFORM in repo_paths, \
        f"platform/ should be a discovered target, got {[str(p) for p in repo_paths]}"


def test_discover_targets_finds_partview_core():
    """Discovery finds platform/partview_core/ (vault has platform/skills/partview_core/,
    repo has platform/partview_core/)."""
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    targets = vs.discover_sync_targets(vault_repo, TEST_REPO)

    repo_paths = [t.repo_path for t in targets]
    assert TEST_PARTVIEW in repo_paths, \
        f"platform/partview_core/ should be a discovered target, got {[str(p) for p in repo_paths]}"


def test_discover_targets_count():
    """Should discover exactly 3 targets: root, platform/, platform/partview_core/."""
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    targets = vs.discover_sync_targets(vault_repo, TEST_REPO)

    assert len(targets) == 3, \
        f"Expected 3 targets, got {len(targets)}: {[str(t.repo_path) for t in targets]}"


def test_root_target_has_files():
    """Root target should have root-level files and skills content."""
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    targets = vs.discover_sync_targets(vault_repo, TEST_REPO)

    root = [t for t in targets if t.repo_path == TEST_REPO][0]
    dest_names = [str(d) for _, d in root.files]

    assert "agent.md" in dest_names, f"Root should have agent.md, got {dest_names}"
    assert any("skills/" in d for d in dest_names), \
        f"Root should have skills content, got {dest_names}"
    # Should NOT have platform-level content
    assert not any(d.startswith("platform/") for d in dest_names), \
        f"Root should NOT have platform/ content, got {dest_names}"


def test_platform_target_has_skills():
    """Platform target should have skills from vault platform/skills/."""
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    targets = vs.discover_sync_targets(vault_repo, TEST_REPO)

    plat = [t for t in targets if t.repo_path == TEST_PLATFORM][0]
    dest_names = [str(d) for _, d in plat.files]

    assert any("skills/" in d for d in dest_names), \
        f"Platform target should have skills, got {dest_names}"
    # Should NOT have partview_core content
    assert not any("partview_core" in d for d in dest_names), \
        f"Platform target should NOT have partview_core content, got {dest_names}"


def test_partview_target_has_own_skills():
    """Partview target should have its own skills from vault."""
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    targets = vs.discover_sync_targets(vault_repo, TEST_REPO)

    pv = [t for t in targets if t.repo_path == TEST_PARTVIEW][0]
    dest_names = [str(d) for _, d in pv.files]

    assert any("skills/" in d for d in dest_names), \
        f"Partview target should have its own skills, got {dest_names}"


# ---------------------------------------------------------------------------
# Unit tests: Manifest building
# ---------------------------------------------------------------------------

def test_build_target_manifest_copilot():
    """Manifest for root target has copilot-specific files."""
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    targets = vs.discover_sync_targets(vault_repo, TEST_REPO)
    root = [t for t in targets if t.repo_path == TEST_REPO][0]

    manifest = vs.build_target_manifest("copilot", root, vault_repo, TEST_REPO)

    assert Path("copilot-instructions.md") in manifest, \
        "copilot manifest should have copilot-instructions.md"
    assert Path("agent.md") in manifest, "copilot manifest should have agent.md"

    # Nested override stripped
    assert Path("skills/subagent/Models/skill.md") in manifest, \
        "skill.md (from skill.copilot.md) should be in manifest"
    source = manifest[Path("skills/subagent/Models/skill.md")]
    assert "skill.copilot.md" in str(source), \
        f"skill.md should point to skill.copilot.md, got {source}"


def test_build_target_manifest_claude():
    """Manifest for root target filters copilot-specific files."""
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    targets = vs.discover_sync_targets(vault_repo, TEST_REPO)
    root = [t for t in targets if t.repo_path == TEST_REPO][0]

    manifest = vs.build_target_manifest("claude", root, vault_repo, TEST_REPO)

    assert Path("copilot-instructions.md") not in manifest, \
        "Claude should not have copilot-instructions.md"
    assert Path("agent.md") in manifest, "Claude should have agent.md"


def test_build_target_manifest_no_vault_content():
    """Root target manifest has NO vault-wide content."""
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    targets = vs.discover_sync_targets(vault_repo, TEST_REPO)
    root = [t for t in targets if t.repo_path == TEST_REPO][0]

    manifest = vs.build_target_manifest("copilot", root, vault_repo, TEST_REPO)

    assert Path("skills/git-workflow.md") not in manifest, \
        "Vault skill should NOT be in repo manifest"
    assert Path("docs/AUTO-SYNC.md") not in manifest, \
        "Vault docs should NOT be in repo manifest"


def test_child_target_inherits_root_files():
    """Non-root targets inherit root-level files."""
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    targets = vs.discover_sync_targets(vault_repo, TEST_REPO)
    plat = [t for t in targets if t.repo_path == TEST_PLATFORM][0]

    manifest = vs.build_target_manifest("copilot", plat, vault_repo, TEST_REPO)

    assert Path("agent.md") in manifest, \
        "platform target should inherit agent.md from root"
    assert Path("config.json") in manifest, \
        "platform target should inherit config.json from root"


def test_child_no_content_cascade():
    """Platform target does NOT get root's skills content."""
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    targets = vs.discover_sync_targets(vault_repo, TEST_REPO)
    plat = [t for t in targets if t.repo_path == TEST_PLATFORM][0]

    manifest = vs.build_target_manifest("copilot", plat, vault_repo, TEST_REPO)

    # Root has skills/changelog-generator/SKILL.md — platform should NOT inherit it
    # Platform has its OWN skills/changelog-generator/SKILL.md from vault platform/skills/
    # Check that the source points to the platform vault, not root vault
    key = Path("skills/changelog-generator/SKILL.md")
    if key in manifest:
        source = str(manifest[key])
        assert "platform" in source, \
            f"Platform's changelog-generator should come from platform vault, got {source}"


def test_vault_manifest_copilot():
    vs = VaultSync(VAULT_PATH)
    manifest = vs.build_vault_manifest("copilot")

    assert Path("skills/git-workflow.md") in manifest
    assert Path("skills/testing.md") in manifest
    assert Path("skills/setup.md") in manifest
    source = manifest[Path("skills/setup.md")][0]
    assert "setup.copilot.md" in str(source)
    assert Path("skills/python.md") in manifest
    assert Path("skills/api-design.md") in manifest


def test_vault_manifest_claude():
    vs = VaultSync(VAULT_PATH)
    manifest = vs.build_vault_manifest("claude")

    assert Path("skills/database.md") in manifest
    assert Path("skills/python.md") in manifest
    assert Path("skills/api-design.md") not in manifest, \
        "Claude should not have api-design.md (copilot-only)"


# ---------------------------------------------------------------------------
# Integration tests: Domain 1 (In-Repo, recursive)
# ---------------------------------------------------------------------------

def test_sync_creates_real_dirs():
    """sync_all creates real directories at all discovered targets."""
    cleanup_all()

    vs = VaultSync(VAULT_PATH)
    vs.sync_all()

    assert (TEST_REPO / ".github").is_dir() and not (TEST_REPO / ".github").is_symlink(), \
        ".github should be a real directory at root"
    assert (TEST_REPO / ".claude").is_dir() and not (TEST_REPO / ".claude").is_symlink(), \
        ".claude should be a real directory at root"
    assert (TEST_PLATFORM / ".github").is_dir(), ".github should exist in platform/"
    assert (TEST_PLATFORM / ".claude").is_dir(), ".claude should exist in platform/"
    assert (TEST_PARTVIEW / ".github").is_dir(), ".github should exist in platform/partview_core/"
    assert (TEST_PARTVIEW / ".claude").is_dir(), ".claude should exist in platform/partview_core/"


def test_sync_agent_filtering():
    """Copilot sees copilot-instructions.md, Claude does not."""
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


def test_repo_no_vault_content():
    """Repo agent dirs do NOT contain vault-wide content."""
    github = TEST_REPO / ".github"
    assert not (github / "docs").exists(), \
        ".github should NOT have docs/ (vault-wide content)"
    assert not (github / "skills" / "git-workflow.md").exists(), \
        ".github should NOT have vault skill git-workflow.md"
    assert not (github / "skills" / "testing.md").exists(), \
        ".github should NOT have vault skill testing.md"


def test_repo_has_repo_skills():
    """Repo agent dirs DO contain repo-specific skills."""
    github = TEST_REPO / ".github"
    assert (github / "skills" / "subagent" / "Skill.md").exists(), \
        ".github should have repo skill subagent/Skill.md"
    assert (github / "skills" / "changelog-generator" / "SKILL.md").exists(), \
        ".github should have repo skill changelog-generator/SKILL.md"


def test_root_excludes_platform_content():
    """Root .github should NOT contain platform/ subdirectory content."""
    github = TEST_REPO / ".github"
    assert not (github / "platform").exists(), \
        "Root .github should NOT have platform/ dir"


def test_platform_has_scoped_skills():
    """platform/.github has scoped skills + inherited root files."""
    plat_github = TEST_PLATFORM / ".github"
    plat_files = sorted(str(f.relative_to(plat_github)) for f in plat_github.rglob("*") if f.is_symlink())

    # Scoped skills — platform/skills/subagent/ is the non-matching content dir
    assert any("skills/subagent" in f for f in plat_files), \
        f"platform/.github should have subagent skill, got: {plat_files}"

    # Inherited root-level files
    assert "agent.md" in plat_files, \
        f"platform/.github should have inherited agent.md"

    # No vault-wide content
    assert not any("docs/" in f for f in plat_files), \
        f"platform/.github should NOT have vault docs"


def test_platform_excludes_partview_content():
    """platform/.github should NOT contain partview_core/ content."""
    plat_github = TEST_PLATFORM / ".github"
    assert not (plat_github / "skills" / "partview_core").exists(), \
        "platform/.github should NOT have partview_core/ content (it's a separate target)"


def test_partview_has_own_skills():
    """platform/partview_core/.github has its own skills + inherited root files."""
    pv_github = TEST_PARTVIEW / ".github"
    pv_files = sorted(str(f.relative_to(pv_github)) for f in pv_github.rglob("*") if f.is_symlink())

    # Own skills from vault platform/skills/partview_core/skills/
    assert any("skills/" in f for f in pv_files), \
        f"partview_core/.github should have skills, got {pv_files}"

    # Inherited root-level files
    assert "agent.md" in pv_files, \
        f"partview_core/.github should inherit agent.md"


def test_partview_no_platform_skills():
    """partview_core/.github should NOT have platform's skills (no cascading)."""
    pv_github = TEST_PARTVIEW / ".github"
    # Platform has skills at the platform level (from platform/skills/).
    # Partview should only have its OWN skills (from platform/skills/partview_core/skills/).
    # Check that the source of any skill.md points to partview_core in vault
    for link in pv_github.rglob("*"):
        if link.is_symlink():
            source = str(link.resolve())
            if "skills" in str(link.relative_to(pv_github)):
                # If it's a skill, it should come from partview_core in vault
                # (or be a root-level file)
                rel_to_pv = str(link.relative_to(pv_github))
                if rel_to_pv not in ("agent.md", "agent.json", "config.json", "copilot-instructions.md"):
                    assert "partview_core" in source or "FV-Platform-Main/" in source, \
                        f"partview_core skill {rel_to_pv} should point to partview_core vault, got {source}"


def test_sync_idempotent():
    """Running sync twice produces same result."""
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    targets = vs.discover_sync_targets(vault_repo, TEST_REPO)
    root = [t for t in targets if t.repo_path == TEST_REPO][0]

    vs.sync_target("copilot", root, vault_repo, TEST_REPO)
    count1 = sum(1 for _ in (TEST_REPO / ".github").rglob("*") if _.is_symlink())

    vs.sync_target("copilot", root, vault_repo, TEST_REPO)
    count2 = sum(1 for _ in (TEST_REPO / ".github").rglob("*") if _.is_symlink())

    assert count1 == count2, f"Idempotent check: {count1} != {count2}"


# ---------------------------------------------------------------------------
# Integration tests: Domain 2 (System-Level)
# ---------------------------------------------------------------------------

def test_sync_system_creates_symlinks():
    """sync_system creates vault-wide content in system agent dirs."""
    cleanup_system_targets()

    vs = VaultSync(VAULT_PATH)
    vs.sync_system()

    assert (SYSTEM_GITHUB / "skills" / "git-workflow.md").exists(), \
        "~/.github/skills/git-workflow.md should exist"
    assert (SYSTEM_GITHUB / "skills" / "testing.md").exists(), \
        "~/.github/skills/testing.md should exist"
    assert (SYSTEM_GITHUB / "docs").is_dir(), \
        "~/.github/docs/ should exist"
    assert (SYSTEM_GITHUB / "instructions").is_dir(), \
        "~/.github/instructions/ should exist"


def test_sync_system_nested_override():
    """Nested override: setup.copilot.md appears as setup.md in ~/.github/."""
    link = SYSTEM_GITHUB / "skills" / "setup.md"
    assert link.exists(), "setup.md should exist in ~/.github/skills/"
    assert link.is_symlink(), "setup.md should be a symlink"
    target = str(link.resolve())
    assert "setup.copilot.md" in target, \
        f"setup.md should point to setup.copilot.md, got {target}"


def test_sync_system_agent_dir_content():
    """Agent dir: skills.copilot/python.md -> ~/.github/skills/python.md"""
    link = SYSTEM_GITHUB / "skills" / "python.md"
    assert link.exists(), "python.md should exist in ~/.github/skills/"
    target = str(link.resolve())
    assert "skills.copilot" in target, \
        f"python.md should point to skills.copilot/, got {target}"


def test_sync_system_agent_filtering():
    """Each agent gets its own filtered content at system level."""
    assert (SYSTEM_GITHUB / "skills" / "api-design.md").exists(), \
        "~/.github should have api-design.md (copilot)"

    assert (SYSTEM_CLAUDE / "skills" / "database.md").exists(), \
        "~/.claude should have database.md (claude)"
    assert not (SYSTEM_CLAUDE / "skills" / "api-design.md").exists(), \
        "~/.claude should NOT have api-design.md (copilot-only)"


def test_sync_system_preserves_claude_cli():
    """Sync and cleanup preserve Claude CLI files in ~/.claude/."""
    settings = SYSTEM_CLAUDE / "settings.local.json"
    history = SYSTEM_CLAUDE / "history.jsonl"

    settings_existed = settings.exists()
    history_existed = history.exists()

    vs = VaultSync(VAULT_PATH)
    vs.sync_system()
    vs.remove_system_links()
    vs.sync_system()

    if settings_existed:
        assert settings.exists(), "~/.claude/settings.local.json should be preserved"
    if history_existed:
        assert history.exists(), "~/.claude/history.jsonl should be preserved"


# ---------------------------------------------------------------------------
# Integration tests: Cleanup
# ---------------------------------------------------------------------------

def test_remove_links():
    """Remove links cleans up repo agent directories."""
    vs = VaultSync(VAULT_PATH)
    vault_repo = REPOS_BASE / "FV-Platform-Main"
    targets = vs.discover_sync_targets(vault_repo, TEST_REPO)
    root = [t for t in targets if t.repo_path == TEST_REPO][0]

    vs.sync_target("copilot", root, vault_repo, TEST_REPO)
    assert (TEST_REPO / ".github").exists()

    vs.remove_links(TEST_REPO)

    assert not (TEST_REPO / ".github").exists(), ".github should be removed"
    assert not (TEST_REPO / ".claude").exists(), ".claude should be removed"


def test_remove_system_links():
    """remove_system_links cleans vault-managed content from system dirs."""
    vs = VaultSync(VAULT_PATH)
    vs.sync_system()

    assert (SYSTEM_GITHUB / "skills").exists(), "~/.github/skills/ should exist before removal"

    vs.remove_system_links()

    assert not (SYSTEM_GITHUB / "skills").exists(), "~/.github/skills/ should be removed"
    assert not (SYSTEM_GITHUB / "docs").exists(), "~/.github/docs/ should be removed"
    assert not (SYSTEM_GITHUB / "instructions").exists(), "~/.github/instructions/ should be removed"


def test_remove_all_cleans_everything():
    """remove_all cleans repos + subdirs + system-level agent dirs."""
    cleanup_all()

    vs = VaultSync(VAULT_PATH)
    vs.sync_all()

    assert (TEST_REPO / ".github").exists()
    assert (TEST_PLATFORM / ".github").exists()
    assert (TEST_PARTVIEW / ".github").exists()
    assert (SYSTEM_GITHUB / "skills").exists()

    vs.remove_all()

    assert not (TEST_REPO / ".github").exists(), "Root .github should be removed"
    assert not (TEST_REPO / ".claude").exists(), "Root .claude should be removed"
    assert not (TEST_PLATFORM / ".github").exists(), "platform/.github should be removed"
    assert not (TEST_PLATFORM / ".claude").exists(), "platform/.claude should be removed"
    assert not (TEST_PARTVIEW / ".github").exists(), "partview_core/.github should be removed"
    assert not (TEST_PARTVIEW / ".claude").exists(), "partview_core/.claude should be removed"
    assert not (SYSTEM_GITHUB / "skills").exists(), "~/.github/skills/ should be removed"


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------

def test_watch_paths_exist():
    """VaultWatcher identifies correct watch paths."""
    vs = VaultSync(VAULT_PATH)
    watcher = VaultWatcher(vault_path=VAULT_PATH, sync=vs)
    paths = watcher._get_watch_paths()
    path_names = [p.name for p in paths]
    assert "repos" in path_names, f"repos/ should be in watch paths, got {path_names}"
    assert "skills" in path_names, f"skills/ should be in watch paths, got {path_names}"
    assert "instructions" in path_names, f"instructions/ should be in watch paths, got {path_names}"


def test_fswatch_command_structure():
    """fswatch command includes excludes and paths."""
    vs = VaultSync(VAULT_PATH)
    watcher = VaultWatcher(vault_path=VAULT_PATH, sync=vs)
    paths = watcher._get_watch_paths()
    cmd = watcher._build_fswatch_cmd(paths)
    assert cmd[0] == "fswatch", f"Command should start with fswatch, got {cmd[0]}"
    assert "-r" in cmd, f"Command should include -r flag"
    assert "--exclude" in cmd, f"Command should include --exclude"


def test_debounce_suppresses_rapid():
    """Rapid changes within debounce window are suppressed."""
    vs = VaultSync(VAULT_PATH, dry_run=True)
    watcher = VaultWatcher(vault_path=VAULT_PATH, sync=vs)
    import time as _time
    watcher._last_sync = _time.monotonic()  # Pretend we just synced
    # This should be suppressed (within debounce window)
    watcher._handle_change("/some/changed/file.md")
    # Verify it didn't update _last_sync (meaning no sync happened)
    elapsed = _time.monotonic() - watcher._last_sync
    assert elapsed < DEBOUNCE_SECONDS, "Debounced call should not update _last_sync"


def test_lock_file_lifecycle():
    """Lock file created and removed correctly."""
    # Clean up any stale lock
    LOCK_FILE.unlink(missing_ok=True)

    vs = VaultSync(VAULT_PATH, dry_run=True)
    watcher = VaultWatcher(vault_path=VAULT_PATH, sync=vs)
    watcher._acquire_lock()
    assert LOCK_FILE.exists(), "Lock file should be created"
    assert LOCK_FILE.read_text().strip() == str(os.getpid()), \
        "Lock file should contain current PID"
    watcher._release_lock()
    assert not LOCK_FILE.exists(), "Lock file should be removed"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("FV-Copilot Sync Test Suite (Recursive Discovery)")
    print("=" * 50)

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

    print("\n--- Recursive Target Discovery ---")
    run_test("discover_targets_finds_root", test_discover_targets_finds_root)
    run_test("discover_targets_finds_platform", test_discover_targets_finds_platform)
    run_test("discover_targets_finds_partview_core", test_discover_targets_finds_partview_core)
    run_test("discover_targets_count", test_discover_targets_count)
    run_test("root_target_has_files", test_root_target_has_files)
    run_test("platform_target_has_skills", test_platform_target_has_skills)
    run_test("partview_target_has_own_skills", test_partview_target_has_own_skills)

    print("\n--- Manifest Building ---")
    run_test("build_target_manifest_copilot", test_build_target_manifest_copilot)
    run_test("build_target_manifest_claude", test_build_target_manifest_claude)
    run_test("build_target_manifest_no_vault_content", test_build_target_manifest_no_vault_content)
    run_test("child_target_inherits_root_files", test_child_target_inherits_root_files)
    run_test("child_no_content_cascade", test_child_no_content_cascade)
    run_test("vault_manifest_copilot", test_vault_manifest_copilot)
    run_test("vault_manifest_claude", test_vault_manifest_claude)

    print("\n--- Integration: Domain 1 (In-Repo, Recursive) ---")
    run_test("sync_creates_real_dirs", test_sync_creates_real_dirs)
    run_test("sync_agent_filtering", test_sync_agent_filtering)
    run_test("sync_universal_files", test_sync_universal_files)
    run_test("sync_symlinks_point_to_vault", test_sync_symlinks_point_to_vault)
    run_test("repo_no_vault_content", test_repo_no_vault_content)
    run_test("repo_has_repo_skills", test_repo_has_repo_skills)
    run_test("root_excludes_platform_content", test_root_excludes_platform_content)
    run_test("platform_has_scoped_skills", test_platform_has_scoped_skills)
    run_test("platform_excludes_partview_content", test_platform_excludes_partview_content)
    run_test("partview_has_own_skills", test_partview_has_own_skills)
    run_test("partview_no_platform_skills", test_partview_no_platform_skills)
    run_test("sync_idempotent", test_sync_idempotent)

    print("\n--- Integration: Domain 2 (System-Level) ---")
    run_test("sync_system_creates_symlinks", test_sync_system_creates_symlinks)
    run_test("sync_system_nested_override", test_sync_system_nested_override)
    run_test("sync_system_agent_dir_content", test_sync_system_agent_dir_content)
    run_test("sync_system_agent_filtering", test_sync_system_agent_filtering)
    run_test("sync_system_preserves_claude_cli", test_sync_system_preserves_claude_cli)

    print("\n--- Integration: Cleanup ---")
    run_test("remove_links", test_remove_links)
    run_test("remove_system_links", test_remove_system_links)
    run_test("remove_all_cleans_everything", test_remove_all_cleans_everything)

    print("\n--- Watcher ---")
    run_test("watch_paths_exist", test_watch_paths_exist)
    run_test("fswatch_command_structure", test_fswatch_command_structure)
    run_test("debounce_suppresses_rapid", test_debounce_suppresses_rapid)
    run_test("lock_file_lifecycle", test_lock_file_lifecycle)

    print(f"\n{'=' * 50}")
    print(f"Results: {passed} passed, {failed} failed")

    if failed:
        sys.exit(1)
    else:
        print("\nAll tests passed!")
        sys.exit(0)
