#!/usr/bin/env python3
"""FV-Copilot vault sync - recursive directory-matching with per-file symlinks.

The vault repo is a superset of the code repo. At each directory level, the
sync compares vault dirs against real repo dirs:

  - Matching dir (exists in real repo)  → recurse, create agent dir there
  - Non-matching dir (vault-only)       → content for current level's agent dir

Domain 1 - In-Repo (recursive discovery):
    Vault: repos/FV-Platform-Main/
      agent.md, config.json            → root .github/ .claude/
      skills/                          → root .github/skills/ (no match in repo)
      platform/                        → MATCHES repo dir → recurse
        skills/                        → platform/.github/skills/
          partview_core/               → MATCHES repo platform/partview_core/ → recurse
            skills/                    → platform/partview_core/.github/skills/

    Root-level files (agent.md etc.) are inherited to all discovered targets.
    Content (skills/) does NOT cascade between targets.

Domain 2 - System-Level (vault-wide content):
    skills/                       -->  ~/.github/skills/  (copilot)
    instructions/                 -->  ~/.github/instructions/
    docs/                         -->  ~/.github/docs/
    Same for ~/.claude/ etc.

Classification priority (highest wins):
    1. AGENT_DIR    - from skills/skills.{agent}/ or instructions/setup.{agent}/
    2. AGENT_NESTED - *.{agent}.* pattern (setup.copilot.md -> setup.md)
    3. AGENT_NAMED  - agent name as word segment (copilot-instructions.md)
    4. UNIVERSAL    - shared across all agents
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VAULT_PATH = Path.home() / "FV-Copilot"
REPOS_BASE = VAULT_PATH / "repos"
AGENTS_INDEX = VAULT_PATH / "agents" / "INDEX.md"
REPOS_INDEX = REPOS_BASE / "INDEX.md"

AGENT_TARGET_MAP: dict[str, str] = {
    "copilot": ".github",
    "claude": ".claude",
    "cursor": ".cursor",
}

EXCLUDE_FILENAMES: set[str] = {
    "command-history-state.json",
    ".DS_Store",
    ".gitkeep",
    ".python-version",
    "INDEX.md",
}

CONTENT_DIRS: list[str] = ["skills", "instructions", "docs"]

# Priority values (higher = wins)
PRIORITY = {
    "UNIVERSAL": 0,
    "AGENT_NAMED": 1,
    "AGENT_NESTED": 2,
    "AGENT_DIR": 3,
}


@dataclass
class SyncTarget:
    """A discovered location where agent dirs should be created."""
    repo_path: Path               # Real repo directory (e.g. ~/git/Repo/platform/)
    files: list[tuple[Path, Path]] = field(default_factory=list)
    # List of (vault_source_abs, dest_relative) pairs.
    # dest_relative is relative to the agent dir at repo_path.


# ---------------------------------------------------------------------------
# VaultSync
# ---------------------------------------------------------------------------

class VaultSync:
    def __init__(self, vault_path: Path = VAULT_PATH, dry_run: bool = False):
        self.vault_path = vault_path
        self.repos_base = vault_path / "repos"
        self.agents_index = vault_path / "agents" / "INDEX.md"
        self.repos_index = self.repos_base / "INDEX.md"
        self.dry_run = dry_run
        self._agents_cache: list[str] | None = None

    # ------------------------------------------------------------------
    # Config parsing
    # ------------------------------------------------------------------

    def get_registered_agents(self) -> list[str]:
        """Parse agents/INDEX.md for active agent names."""
        if self._agents_cache is not None:
            return self._agents_cache

        if not self.agents_index.exists():
            print(f"Error: {self.agents_index} not found", file=sys.stderr)
            sys.exit(1)

        agents: list[str] = []
        in_active = False
        for line in self.agents_index.read_text().splitlines():
            if line.strip().startswith("## Active Agents"):
                in_active = True
                continue
            if in_active and line.strip().startswith("##"):
                break
            if in_active and line.strip().startswith("- "):
                name = line.strip().removeprefix("- ").strip()
                if name and name[0].isalpha():
                    agents.append(name)

        self._agents_cache = agents
        return agents

    def get_managed_repos(self) -> list[Path]:
        """Parse repos/INDEX.md for repo paths.

        Returns list of repo paths. Subdirectory targets are auto-discovered
        by comparing vault structure against real repo structure.
        """
        if not self.repos_index.exists():
            print(f"Error: {self.repos_index} not found", file=sys.stderr)
            sys.exit(1)

        repos: list[Path] = []
        for line in self.repos_index.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("~") or stripped.startswith("/"):
                repos.append(Path(os.path.expanduser(stripped)))
        return repos

    def get_agent_target(self, agent: str) -> str:
        """Map agent name to target directory name."""
        return AGENT_TARGET_MAP.get(agent, f".{agent}")

    # ------------------------------------------------------------------
    # File classification
    # ------------------------------------------------------------------

    def classify_file(
        self, filepath: Path, base_path: Path, agent: str
    ) -> tuple[str, Path]:
        """Classify a file for a given agent.

        Returns (classification, dest_relative_path).
        classification is one of: UNIVERSAL, AGENT_NESTED, AGENT_NAMED,
                                  AGENT_DIR, SKIP
        """
        filename = filepath.name
        rel = filepath.relative_to(base_path)

        # Exclusions
        if filename in EXCLUDE_FILENAMES:
            return ("SKIP", rel)

        all_agents = self.get_registered_agents()

        # --- Check if file is inside an agent-specific directory ---
        # e.g., skills/skills.copilot/python.md or instructions/setup.claude/x.md
        for part in rel.parts[:-1]:  # check directory components
            for a in all_agents:
                if part.endswith(f".{a}"):
                    if a == agent:
                        return ("AGENT_DIR", self._remap_agent_dir(rel, a))
                    else:
                        return ("SKIP", rel)

        # --- Check nested override pattern: name.agent.ext ---
        for a in all_agents:
            pattern = re.compile(rf"\.{re.escape(a)}\.", re.IGNORECASE)
            if pattern.search(filename):
                if a == agent:
                    stripped = re.sub(rf"\.{re.escape(a)}\.", ".", filename)
                    return ("AGENT_NESTED", rel.parent / stripped)
                else:
                    return ("SKIP", rel)

        # --- Check agent name as word segment in filename ---
        for a in all_agents:
            pattern = re.compile(
                rf"(^|[-_.]){re.escape(a)}([-_.]|$)", re.IGNORECASE
            )
            if pattern.search(filename):
                if a == agent:
                    return ("AGENT_NAMED", rel)
                else:
                    return ("SKIP", rel)

        return ("UNIVERSAL", rel)

    def _remap_agent_dir(self, rel: Path, agent: str) -> Path:
        """Remap a path inside an agent directory to its canonical location.

        Examples:
            skills/skills.copilot/python.md -> skills/python.md
            instructions/setup.copilot/x.md -> instructions/setup/x.md
        """
        parts = list(rel.parts)
        new_parts: list[str] = []

        for part in parts:
            if part.endswith(f".{agent}"):
                base = part.removesuffix(f".{agent}")
                # If agent dir name matches parent (skills/skills.copilot),
                # skip it (files go directly into skills/)
                if new_parts and new_parts[-1] == base:
                    continue
                # Otherwise create a subdir (instructions/setup.copilot -> instructions/setup)
                new_parts.append(base)
            else:
                new_parts.append(part)

        return Path(*new_parts) if new_parts else rel

    # ------------------------------------------------------------------
    # Recursive target discovery
    # ------------------------------------------------------------------

    def discover_sync_targets(
        self, vault_dir: Path, repo_dir: Path,
        repo_root: Path | None = None,
    ) -> list[SyncTarget]:
        """Recursively discover where to create agent dirs.

        Walks the vault directory tree. At each level, compares vault subdirs
        against real repo subdirs:
          - Match (exists in real repo) → new target, recurse
          - No match (vault-only) → content for current target's agent dir

        Args:
            vault_dir: Current vault directory being scanned
            repo_dir: Corresponding real repo directory
            repo_root: The repo root (for tracking where we started). If None,
                      this IS the root.

        Returns list of SyncTarget objects.
        """
        if not vault_dir.exists():
            return []

        if repo_root is None:
            repo_root = repo_dir

        target = SyncTarget(repo_path=repo_dir)
        all_targets = [target]

        self._walk_vault(vault_dir, repo_dir, repo_root, target, all_targets, Path())

        return all_targets

    def _walk_vault(
        self,
        vault_dir: Path,
        repo_dir: Path,
        repo_root: Path,
        current_target: SyncTarget,
        all_targets: list[SyncTarget],
        content_prefix: Path,
        content_depth: int = 0,
    ) -> None:
        """Recursive walk of vault directory.

        Args:
            vault_dir: Current vault directory being scanned
            repo_dir: The real repo dir that current_target corresponds to
            repo_root: The repo root (to check matches against)
            current_target: SyncTarget we're accumulating files into
            all_targets: Global list of all discovered targets
            content_prefix: Path prefix for files relative to agent dir
                           (e.g. Path("skills/subagent") when we're inside
                           vault's skills/subagent/ which doesn't match repo)
            content_depth: How deep we are inside a content tree.
                          0 = at matched level, 1 = direct child of content dir,
                          2+ = deeper content (no more matching).
        """
        for entry in sorted(vault_dir.iterdir()):
            if entry.name.startswith("."):
                continue

            if entry.is_file():
                # Files go to current target with the content prefix
                dest_rel = content_prefix / entry.name if content_prefix != Path() else Path(entry.name)
                current_target.files.append((entry, dest_rel))

            elif entry.is_dir():
                # Check for repo dir match at:
                #   depth 0: direct children of a matched vault dir
                #   depth 1: direct children of a content dir (e.g. skills/partview_core/)
                # At depth 2+, we're deep in content structure → no more matching
                can_match = (content_depth <= 1)

                if can_match:
                    real_counterpart = current_target.repo_path / entry.name
                    if real_counterpart.is_dir():
                        # MATCH: real repo dir → new target, recurse
                        new_target = SyncTarget(repo_path=real_counterpart)
                        all_targets.append(new_target)
                        self._walk_vault(
                            entry, real_counterpart, repo_root,
                            new_target, all_targets, Path(), 0,
                        )
                        continue

                # NO MATCH: vault-only dir → content for current target
                in_content = (content_prefix != Path())
                new_prefix = content_prefix / entry.name if in_content else Path(entry.name)
                new_depth = content_depth + 1 if content_depth > 0 else 1
                self._walk_vault(
                    entry, repo_dir, repo_root,
                    current_target, all_targets, new_prefix, new_depth,
                )

    # ------------------------------------------------------------------
    # Manifest building
    # ------------------------------------------------------------------

    def build_target_manifest(
        self, agent: str, target: SyncTarget,
        vault_repo_root: Path, repo_root: Path,
    ) -> dict[Path, Path]:
        """Build classified manifest for a discovered target.

        Takes the raw files from target discovery, applies agent classification
        (filtering, priority, renaming), and adds inherited root-level files.

        Args:
            target: Discovered sync target with files list
            vault_repo_root: Vault repo dir (for root-level file inheritance)
            repo_root: Real repo root path (to detect if target IS the root)

        Returns {dest_relative: source_absolute}.
        """
        manifest: dict[Path, tuple[Path, int]] = {}

        # Classify each file discovered for this target
        for source_abs, dest_rel in target.files:
            classification, classified_dest = self._classify_with_dest(
                source_abs, dest_rel, agent
            )
            if classification == "SKIP":
                continue

            pri = PRIORITY[classification]
            if classified_dest not in manifest or pri > manifest[classified_dest][1]:
                manifest[classified_dest] = (source_abs, pri)

        # Inherit root-level files if this isn't the root target
        is_root = (target.repo_path == repo_root)
        if not is_root and vault_repo_root.exists():
            for f in sorted(vault_repo_root.iterdir()):
                if f.is_file():
                    classification, classified_dest = self._classify_with_dest(
                        f, Path(f.name), agent
                    )
                    if classification == "SKIP":
                        continue
                    pri = PRIORITY[classification]
                    # Don't override if target already has this file
                    if classified_dest not in manifest:
                        manifest[classified_dest] = (f, pri)

        return {dest: source for dest, (source, _pri) in manifest.items()}

    def _classify_with_dest(
        self, source_abs: Path, dest_rel: Path, agent: str
    ) -> tuple[str, Path]:
        """Classify a file using its destination-relative path.

        This handles agent filtering, nested overrides, agent dirs, etc.
        using the dest_rel path for pattern matching.
        """
        filename = source_abs.name

        # Exclusions
        if filename in EXCLUDE_FILENAMES:
            return ("SKIP", dest_rel)

        all_agents = self.get_registered_agents()

        # --- Check if file is inside an agent-specific directory ---
        for part in dest_rel.parts[:-1]:
            for a in all_agents:
                if part.endswith(f".{a}"):
                    if a == agent:
                        return ("AGENT_DIR", self._remap_agent_dir(dest_rel, a))
                    else:
                        return ("SKIP", dest_rel)

        # --- Check nested override pattern: name.agent.ext ---
        for a in all_agents:
            pattern = re.compile(rf"\.{re.escape(a)}\.", re.IGNORECASE)
            if pattern.search(filename):
                if a == agent:
                    stripped = re.sub(rf"\.{re.escape(a)}\.", ".", filename)
                    return ("AGENT_NESTED", dest_rel.parent / stripped)
                else:
                    return ("SKIP", dest_rel)

        # --- Check agent name as word segment in filename ---
        for a in all_agents:
            pattern = re.compile(
                rf"(^|[-_.]){re.escape(a)}([-_.]|$)", re.IGNORECASE
            )
            if pattern.search(filename):
                if a == agent:
                    return ("AGENT_NAMED", dest_rel)
                else:
                    return ("SKIP", dest_rel)

        return ("UNIVERSAL", dest_rel)

    def build_vault_manifest(self, agent: str) -> dict[Path, tuple[Path, int]]:
        """Build manifest from vault-wide content directories.

        Scans skills/, instructions/, docs/ with agent-specific filtering.
        Returns {dest_relative: (source_absolute, priority)}.
        """
        manifest: dict[Path, tuple[Path, int]] = {}

        for content_dir_name in CONTENT_DIRS:
            content_dir = self.vault_path / content_dir_name
            if not content_dir.is_dir():
                continue

            for filepath in content_dir.rglob("*"):
                if not filepath.is_file():
                    continue

                classification, dest_rel = self.classify_file(
                    filepath, self.vault_path, agent
                )
                if classification == "SKIP":
                    continue

                pri = PRIORITY[classification]
                if dest_rel not in manifest or pri > manifest[dest_rel][1]:
                    manifest[dest_rel] = (filepath, pri)

        return manifest

    # ------------------------------------------------------------------
    # Sync operations
    # ------------------------------------------------------------------

    def sync_target(
        self, agent: str, target: SyncTarget,
        vault_repo_root: Path, repo_root: Path,
    ) -> None:
        """Create per-file symlinks in target.repo_path/<agent_target>/.

        Args:
            target: Discovered sync target with files list
            vault_repo_root: Root vault repo dir for inheriting root-level files
            repo_root: Real repo root path (to detect if target IS the root)
        """
        target_name = self.get_agent_target(agent)
        target_dir = target.repo_path / target_name

        print(f"  [{agent}] -> {target_name}/")

        # Remove old directory symlink (legacy mode)
        if target_dir.is_symlink():
            print(f"    Removing old directory symlink")
            if not self.dry_run:
                target_dir.unlink()

        # Create real directory
        if not self.dry_run:
            target_dir.mkdir(exist_ok=True)

        manifest = self.build_target_manifest(agent, target, vault_repo_root, repo_root)

        created: set[Path] = set()
        count_new = 0
        count_skip = 0

        for dest_rel, source in sorted(manifest.items()):
            dest = target_dir / dest_rel
            created.add(dest)

            # Create parent directories
            if not self.dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)

            # Already correct?
            if dest.is_symlink():
                try:
                    if dest.resolve() == source.resolve():
                        count_skip += 1
                        continue
                except OSError:
                    pass  # broken symlink, will be replaced

            # Remove existing file/symlink
            if dest.exists() or dest.is_symlink():
                if not self.dry_run:
                    dest.unlink()

            # Create symlink
            if not self.dry_run:
                dest.symlink_to(source)
            count_new += 1

        # Clean stale symlinks
        count_stale = 0
        if target_dir.exists():
            for link in list(target_dir.rglob("*")):
                if link.is_symlink() and link not in created:
                    count_stale += 1
                    if not self.dry_run:
                        link.unlink()

            # Remove empty directories (bottom-up)
            for dirpath in sorted(
                (d for d in target_dir.rglob("*") if d.is_dir()),
                reverse=True,
            ):
                try:
                    if not any(dirpath.iterdir()):
                        if not self.dry_run:
                            dirpath.rmdir()
                except OSError:
                    pass

        print(
            f"    {count_new} created, {count_skip} unchanged, "
            f"{count_stale} stale removed "
            f"({len(manifest)} total files)"
        )

    def sync_all(
        self, agent: str | None = None, repo: str | None = None
    ) -> None:
        """Sync all (or specific) agents and repos.

        Two sync domains:
        1. In-repo content: recursive discovery -> repo agent dirs
        2. System content: vault-wide content -> ~/{agent_target}/
        """
        agents = [agent] if agent else self.get_registered_agents()
        repos = self.get_managed_repos()

        # Filter to specific repo if requested
        if repo:
            repo_path = Path(os.path.expanduser(repo))
            repos = [
                r for r in repos
                if r == repo_path or r.name == repo_path.name
            ]
            if not repos:
                # Try as bare name
                repo_path = Path.home() / "git" / repo
                if repo_path.exists():
                    repos = [repo_path]

        # --- Domain 1: In-repo content (recursive discovery) ---
        if not repos:
            print("No matching repos found.")
        else:
            for repo_path in repos:
                repo_name = repo_path.name
                vault_repo = self.repos_base / repo_name

                if not repo_path.exists():
                    print(f"  Repo not found: {repo_path}")
                    continue

                targets = self.discover_sync_targets(vault_repo, repo_path)

                for target in targets:
                    label = repo_name
                    if target.repo_path != repo_path:
                        rel = target.repo_path.relative_to(repo_path)
                        label = f"{repo_name}/{rel}"
                    print(f"Syncing {label}:")

                    for a in agents:
                        if a not in self.get_registered_agents():
                            print(f"  Agent '{a}' not registered, skipping")
                            continue
                        self.sync_target(a, target, vault_repo, repo_path)

        # --- Domain 2: System-level vault-wide content ---
        print(f"\nSyncing system-level agent config:")
        self.sync_system(agent=agent)

        print("\nSync complete.")

    def sync_system(self, agent: str | None = None) -> None:
        """Sync vault-wide content (skills/, instructions/, docs/) to ~/{agent_target}/.

        This is the 'outside-repo' domain. Content from the vault's top-level
        content directories goes to system-level agent config.
        """
        agents = [agent] if agent else self.get_registered_agents()

        for a in agents:
            if a not in self.get_registered_agents():
                print(f"  Agent '{a}' not registered, skipping")
                continue

            target_name = self.get_agent_target(a)
            target_dir = Path.home() / target_name

            print(f"  [{a}] -> ~/{target_name}/")

            vault_manifest = self.build_vault_manifest(a)

            # Create target dir
            if not self.dry_run:
                target_dir.mkdir(exist_ok=True)

            created: set[Path] = set()
            count_new = 0
            count_skip = 0

            for dest_rel, (source, _pri) in sorted(vault_manifest.items()):
                dest = target_dir / dest_rel
                created.add(dest)

                if not self.dry_run:
                    dest.parent.mkdir(parents=True, exist_ok=True)

                # Already correct?
                if dest.is_symlink():
                    try:
                        if dest.resolve() == source.resolve():
                            count_skip += 1
                            continue
                    except OSError:
                        pass

                if dest.exists() or dest.is_symlink():
                    if not self.dry_run:
                        dest.unlink()

                if not self.dry_run:
                    dest.symlink_to(source)
                count_new += 1

            # Clean stale symlinks - ONLY under CONTENT_DIRS subdirs
            # (never touch other files in e.g. ~/.claude/)
            count_stale = 0
            for content_dir_name in CONTENT_DIRS:
                content_target = target_dir / content_dir_name
                if not content_target.exists():
                    continue

                for link in list(content_target.rglob("*")):
                    if link.is_symlink() and link not in created:
                        count_stale += 1
                        if not self.dry_run:
                            link.unlink()

                # Remove empty dirs bottom-up within content dir
                for dirpath in sorted(
                    (d for d in content_target.rglob("*") if d.is_dir()),
                    reverse=True,
                ):
                    try:
                        if not any(dirpath.iterdir()):
                            if not self.dry_run:
                                dirpath.rmdir()
                    except OSError:
                        pass

                # Remove content dir itself if empty
                try:
                    if not any(content_target.iterdir()):
                        if not self.dry_run:
                            content_target.rmdir()
                except OSError:
                    pass

            print(
                f"    {count_new} created, {count_skip} unchanged, "
                f"{count_stale} stale removed "
                f"({len(vault_manifest)} total files)"
            )

    def remove_links(self, repo_path: Path) -> None:
        """Remove all agent target directories from a repo."""
        for agent in self.get_registered_agents():
            target_name = self.get_agent_target(agent)
            target_dir = repo_path / target_name

            if target_dir.is_symlink():
                print(f"  Removing directory symlink: {target_dir.name}")
                if not self.dry_run:
                    target_dir.unlink()
            elif target_dir.is_dir():
                # Count and remove only symlinks inside
                count = 0
                for f in list(target_dir.rglob("*")):
                    if f.is_symlink():
                        count += 1
                        if not self.dry_run:
                            f.unlink()

                # Remove empty directories bottom-up
                for d in sorted(
                    (x for x in target_dir.rglob("*") if x.is_dir()),
                    reverse=True,
                ):
                    try:
                        if not any(d.iterdir()):
                            if not self.dry_run:
                                d.rmdir()
                    except OSError:
                        pass

                # Remove the target dir itself if empty
                try:
                    if target_dir.is_dir() and not any(target_dir.iterdir()):
                        if not self.dry_run:
                            target_dir.rmdir()
                except OSError:
                    pass

                print(f"  Removed {count} symlinks from {target_dir.name}/")

    def remove_system_links(self) -> None:
        """Remove vault-managed symlinks from system-level agent dirs.

        Only touches CONTENT_DIRS (skills/, instructions/, docs/) subdirs.
        Never removes or modifies other files (e.g. ~/.claude/settings.local.json).
        """
        for agent in self.get_registered_agents():
            target_name = self.get_agent_target(agent)
            target_dir = Path.home() / target_name

            if not target_dir.is_dir():
                continue

            count = 0
            for content_dir_name in CONTENT_DIRS:
                content_target = target_dir / content_dir_name
                if not content_target.exists():
                    continue

                for f in list(content_target.rglob("*")):
                    if f.is_symlink():
                        count += 1
                        if not self.dry_run:
                            f.unlink()

                # Remove empty dirs bottom-up
                for d in sorted(
                    (x for x in content_target.rglob("*") if x.is_dir()),
                    reverse=True,
                ):
                    try:
                        if not any(d.iterdir()):
                            if not self.dry_run:
                                d.rmdir()
                    except OSError:
                        pass

                # Remove content dir itself if empty
                try:
                    if content_target.is_dir() and not any(content_target.iterdir()):
                        if not self.dry_run:
                            content_target.rmdir()
                except OSError:
                    pass

            if count > 0:
                print(f"  Removed {count} symlinks from ~/{target_name}/")

    def remove_all(self, repo: str | None = None) -> None:
        """Remove links from all repos + system-level agent dirs."""
        repos = self.get_managed_repos()
        if repo:
            repo_path = Path(os.path.expanduser(repo))
            repos = [
                r for r in repos
                if r == repo_path or r.name == repo_path.name
            ]

        # Domain 1: In-repo agent dirs (use discovery to find all targets)
        for repo_path in repos:
            if not repo_path.exists():
                continue
            vault_repo = self.repos_base / repo_path.name
            targets = self.discover_sync_targets(vault_repo, repo_path)
            for target in targets:
                label = repo_path.name
                if target.repo_path != repo_path:
                    rel = target.repo_path.relative_to(repo_path)
                    label = f"{repo_path.name}/{rel}"
                print(f"Cleaning {label}:")
                self.remove_links(target.repo_path)

        # Domain 2: System-level agent dirs
        print(f"Cleaning system-level agent config:")
        self.remove_system_links()

    def merge_and_relink(
        self, target: SyncTarget, vault_repo_root: Path,
        repo_root: Path, agent: str | None = None,
    ) -> None:
        """If an agent target dir exists as a real dir (post-git-merge),
        merge new files into vault, then re-sync."""
        agents = [agent] if agent else self.get_registered_agents()

        for a in agents:
            target_name = self.get_agent_target(a)
            target_dir = target.repo_path / target_name

            # Only act if it's a real directory (not a symlink, not managed)
            if not target_dir.is_dir() or target_dir.is_symlink():
                continue

            # Check if it has non-symlink files (real files from git)
            real_files = [
                f for f in target_dir.rglob("*")
                if f.is_file() and not f.is_symlink()
            ]

            if not real_files:
                continue

            print(f"  [{a}] Found {len(real_files)} real files in {target_name}/")
            print(f"    Merging new files into vault...")

            for real_file in real_files:
                rel = real_file.relative_to(target_dir)
                vault_file = vault_repo_root / rel

                if not vault_file.exists():
                    print(f"    + {rel} (new -> vault)")
                    if not self.dry_run:
                        vault_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(real_file, vault_file)
                else:
                    print(f"    = {rel} (vault wins)")

            # Remove the real directory
            print(f"    Removing real {target_name}/ directory")
            if not self.dry_run:
                shutil.rmtree(target_dir)

            # Re-sync
            print(f"    Re-syncing...")
            self.sync_target(a, target, vault_repo_root, repo_root)


# ---------------------------------------------------------------------------
# File watcher
# ---------------------------------------------------------------------------

LOCK_FILE = Path("/tmp/fv-copilot-watcher.pid")
DEBOUNCE_SECONDS = 0.5
FSWATCH_EXCLUDES = [r"\.git", r"\.DS_Store", r"node_modules", r"__pycache__"]


class VaultWatcher:
    """Watch vault directories and re-sync on changes (requires fswatch)."""

    def __init__(
        self,
        vault_path: Path,
        sync: VaultSync,
        agent: str | None = None,
        repo: str | None = None,
    ):
        self.vault_path = vault_path
        self.sync = sync
        self.agent = agent
        self.repo = repo
        self._process: subprocess.Popen | None = None
        self._last_sync: float = 0.0

    def _check_fswatch(self) -> None:
        if shutil.which("fswatch") is None:
            print("Error: fswatch not installed. Run: brew install fswatch",
                  file=sys.stderr)
            sys.exit(1)

    def _acquire_lock(self) -> None:
        if LOCK_FILE.exists():
            try:
                old_pid = int(LOCK_FILE.read_text().strip())
                os.kill(old_pid, 0)  # Check if still running
                print(f"Error: Watcher already running (PID: {old_pid})",
                      file=sys.stderr)
                sys.exit(1)
            except (ProcessLookupError, ValueError):
                LOCK_FILE.unlink(missing_ok=True)
        LOCK_FILE.write_text(str(os.getpid()))

    def _release_lock(self) -> None:
        LOCK_FILE.unlink(missing_ok=True)

    def _get_watch_paths(self) -> list[Path]:
        paths: list[Path] = []
        repos_base = self.vault_path / "repos"
        if repos_base.is_dir():
            paths.append(repos_base)
        for content_dir in CONTENT_DIRS:
            d = self.vault_path / content_dir
            if d.is_dir():
                paths.append(d)
        # Also watch agent-specific dirs (skills.copilot/, etc.)
        for entry in sorted(self.vault_path.iterdir()):
            if entry.is_dir() and "." in entry.name:
                base, _, _ = entry.name.partition(".")
                if base in CONTENT_DIRS:
                    paths.append(entry)
        return paths

    def _build_fswatch_cmd(self, paths: list[Path]) -> list[str]:
        cmd = ["fswatch", "-r"]
        for exclude in FSWATCH_EXCLUDES:
            cmd.extend(["--exclude", exclude])
        cmd.extend(str(p) for p in paths)
        return cmd

    def _handle_change(self, changed_path: str) -> None:
        now = time.monotonic()
        if now - self._last_sync < DEBOUNCE_SECONDS:
            return
        self._last_sync = now
        print(f"\nChange detected: {Path(changed_path).name}")
        print("Re-syncing...")
        try:
            self.sync.sync_all(agent=self.agent, repo=self.repo)
        except Exception as e:
            print(f"Sync error: {e}", file=sys.stderr)

    def _cleanup(self, signum: int = 0, frame: object = None) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._release_lock()
        sys.exit(0)

    def run(self) -> None:
        self._check_fswatch()
        self._acquire_lock()

        signal.signal(signal.SIGINT, self._cleanup)
        signal.signal(signal.SIGTERM, self._cleanup)

        watch_paths = self._get_watch_paths()
        if not watch_paths:
            print("No directories to watch.", file=sys.stderr)
            self._release_lock()
            sys.exit(1)

        # Initial sync
        print("Running initial sync...")
        self.sync.sync_all(agent=self.agent, repo=self.repo)

        print(f"\nWatching for changes in:")
        for p in watch_paths:
            print(f"  {p}")
        print("Press Ctrl+C to stop.\n")

        cmd = self._build_fswatch_cmd(watch_paths)
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
            for line in self._process.stdout:
                changed = line.strip()
                if changed:
                    self._handle_change(changed)
        except Exception as e:
            print(f"Watcher error: {e}", file=sys.stderr)
        finally:
            self._cleanup()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="FV-Copilot vault sync tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 sync.py --mode symlink                    # Sync all agents, all repos
  python3 sync.py --mode symlink --agent copilot    # Sync copilot only
  python3 sync.py --mode symlink --repo FV-Platform-Main
  python3 sync.py --clean                           # Remove all agent dirs
  python3 sync.py --merge                           # Post-git-merge recovery
  python3 sync.py --watch                           # Watch and auto-sync
  python3 sync.py --dry-run --mode symlink          # Preview changes
        """,
    )
    parser.add_argument(
        "--mode",
        choices=["symlink"],
        default="symlink",
        help="Sync mode (default: symlink)",
    )
    parser.add_argument("--agent", help="Specific agent to sync")
    parser.add_argument("--repo", help="Specific repo (name or path)")
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview without changes"
    )
    parser.add_argument(
        "--clean", action="store_true", help="Remove all agent directories"
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="Merge real dirs into vault and re-sync (post-git-merge)",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch vault for changes and auto-sync (requires fswatch)",
    )

    args = parser.parse_args()

    vs = VaultSync(dry_run=args.dry_run)

    if args.dry_run:
        print("DRY RUN - no changes will be made\n")

    if args.watch:
        watcher = VaultWatcher(vs.vault_path, vs,
                               agent=args.agent, repo=args.repo)
        watcher.run()
    elif args.clean:
        vs.remove_all(repo=args.repo)
    elif args.merge:
        repos = vs.get_managed_repos()
        if args.repo:
            repo_path = Path(os.path.expanduser(args.repo))
            repos = [
                r for r in repos
                if r == repo_path or r.name == repo_path.name
            ]
        for repo_path in repos:
            if not repo_path.exists():
                continue
            vault_repo = vs.repos_base / repo_path.name
            targets = vs.discover_sync_targets(vault_repo, repo_path)

            for target in targets:
                label = repo_path.name
                if target.repo_path != repo_path:
                    rel = target.repo_path.relative_to(repo_path)
                    label = f"{repo_path.name}/{rel}"
                print(f"Merge & relink: {label}")
                vs.merge_and_relink(target, vault_repo, repo_path, agent=args.agent)
    else:
        vs.sync_all(agent=args.agent, repo=args.repo)


if __name__ == "__main__":
    main()
