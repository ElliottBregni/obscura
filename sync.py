#!/usr/bin/env python3
"""FV-Copilot vault sync - creates per-file symlinks with agent-specific filtering.

Architecture:
    Vault (~/FV-Copilot/)          Target Repo (~/git/FV-Platform-Main/)
    ========================       ========================================
    repos/FV-Platform-Main/   -->  .github/  (copilot, per-file symlinks)
      copilot-instructions.md      .claude/  (claude, per-file symlinks)
      agent.md
      skills/

    skills/                   -->  .github/skills/  (filtered per agent)
      setup.md (universal)         .claude/skills/
      setup.copilot.md (override)
      skills.copilot/python.md

    instructions/             -->  .github/instructions/
      x.md, x.copilot.md          .claude/instructions/

    docs/                     -->  .github/docs/
                                   .claude/docs/

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
import shutil
import sys
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
        """Parse repos/INDEX.md for repo paths (with ~ expansion)."""
        if not self.repos_index.exists():
            print(f"Error: {self.repos_index} not found", file=sys.stderr)
            sys.exit(1)

        repos: list[Path] = []
        for line in self.repos_index.read_text().splitlines():
            line = line.strip()
            if line.startswith("~") or line.startswith("/"):
                repos.append(Path(os.path.expanduser(line)))
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
    # Manifest building
    # ------------------------------------------------------------------

    def build_repo_manifest(
        self, agent: str, vault_repo: Path
    ) -> dict[Path, tuple[Path, int]]:
        """Build manifest from repo-specific vault content.

        Returns {dest_relative: (source_absolute, priority)}.
        """
        manifest: dict[Path, tuple[Path, int]] = {}

        if not vault_repo.exists():
            return manifest

        for filepath in vault_repo.rglob("*"):
            if not filepath.is_file():
                continue

            classification, dest_rel = self.classify_file(
                filepath, vault_repo, agent
            )
            if classification == "SKIP":
                continue

            pri = PRIORITY[classification]
            if dest_rel not in manifest or pri > manifest[dest_rel][1]:
                manifest[dest_rel] = (filepath, pri)

        return manifest

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

    def build_full_manifest(
        self, agent: str, vault_repo: Path
    ) -> dict[Path, Path]:
        """Merge repo + vault manifests. Returns {dest_relative: source_absolute}."""
        # Start with vault-wide content
        combined: dict[Path, tuple[Path, int]] = self.build_vault_manifest(agent)

        # Layer repo content on top (repo wins for same dest at same priority)
        repo_manifest = self.build_repo_manifest(agent, vault_repo)
        for dest_rel, (source, pri) in repo_manifest.items():
            if dest_rel not in combined or pri >= combined[dest_rel][1]:
                combined[dest_rel] = (source, pri)

        # Strip priority from output
        return {dest: source for dest, (source, _pri) in combined.items()}

    # ------------------------------------------------------------------
    # Sync operations
    # ------------------------------------------------------------------

    def sync_repo(self, agent: str, repo_path: Path, vault_repo: Path) -> None:
        """Create per-file symlinks in repo_path/<agent_target>/."""
        target_name = self.get_agent_target(agent)
        target_dir = repo_path / target_name

        print(f"  [{agent}] -> {target_name}/")

        # Remove old directory symlink (legacy mode)
        if target_dir.is_symlink():
            print(f"    Removing old directory symlink")
            if not self.dry_run:
                target_dir.unlink()

        # Create real directory
        if not self.dry_run:
            target_dir.mkdir(exist_ok=True)

        manifest = self.build_full_manifest(agent, vault_repo)

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
        """Sync all (or specific) agents and repos."""
        agents = [agent] if agent else self.get_registered_agents()
        repos = self.get_managed_repos()

        # Filter to specific repo if requested
        if repo:
            repo_path = Path(os.path.expanduser(repo))
            repos = [r for r in repos if r == repo_path or r.name == repo_path.name]
            if not repos:
                # Try as bare name
                repo_path = Path.home() / "git" / repo
                if repo_path.exists():
                    repos = [repo_path]

        if not repos:
            print("No matching repos found.")
            return

        for repo_path in repos:
            repo_name = repo_path.name
            vault_repo = self.repos_base / repo_name

            if not repo_path.exists():
                print(f"  Repo not found: {repo_path}")
                continue

            print(f"Syncing {repo_name}:")
            for a in agents:
                if a not in self.get_registered_agents():
                    print(f"  Agent '{a}' not registered, skipping")
                    continue
                self.sync_repo(a, repo_path, vault_repo)

        print("\nSync complete.")

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

    def remove_all(self, repo: str | None = None) -> None:
        """Remove links from all or specific repos."""
        repos = self.get_managed_repos()
        if repo:
            repo_path = Path(os.path.expanduser(repo))
            repos = [r for r in repos if r == repo_path or r.name == repo_path.name]

        for repo_path in repos:
            if repo_path.exists():
                print(f"Cleaning {repo_path.name}:")
                self.remove_links(repo_path)

    def merge_and_relink(
        self, repo_path: Path, vault_repo: Path, agent: str | None = None
    ) -> None:
        """If an agent target dir exists as a real dir (post-git-merge),
        merge new files into vault, then re-sync."""
        agents = [agent] if agent else self.get_registered_agents()

        for a in agents:
            target_name = self.get_agent_target(a)
            target_dir = repo_path / target_name

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
                vault_file = vault_repo / rel

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
            self.sync_repo(a, repo_path, vault_repo)


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

    args = parser.parse_args()

    vs = VaultSync(dry_run=args.dry_run)

    if args.dry_run:
        print("DRY RUN - no changes will be made\n")

    if args.clean:
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
            if repo_path.exists():
                vault_repo = vs.repos_base / repo_path.name
                print(f"Merge & relink: {repo_path.name}")
                vs.merge_and_relink(repo_path, vault_repo, agent=args.agent)
    else:
        vs.sync_all(agent=args.agent, repo=args.repo)


if __name__ == "__main__":
    main()
