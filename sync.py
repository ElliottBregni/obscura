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

Variant selection (post-manifest filtering):
    After building the manifest, VariantSelector filters by active sync profile:
    - Model variants: setup.claude.opus.md replaces setup.claude.md when model=opus
    - Role overlays:  roles/reviewer.md included only when role=reviewer
    Profile read from .sync-profile.yml in vault root (per-repo overrides supported).
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
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

Classification = Literal["UNIVERSAL", "AGENT_NAMED", "AGENT_NESTED", "AGENT_DIR", "SKIP"]
_AgentTargetFn = Callable[[str], str]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VAULT_PATH = Path.home() / "obscura"
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

KNOWN_MODELS: set[str] = {"opus", "sonnet", "haiku"}

SYNC_PROFILE_FILE = ".sync-profile.yml"

PRIORITY: dict[str, int] = {
    "UNIVERSAL": 0,
    "AGENT_NAMED": 1,
    "AGENT_NESTED": 2,
    "AGENT_DIR": 3,
}


@dataclass
class SyncTarget:
    """A discovered location where agent dirs should be created."""
    repo_path: Path
    files: list[tuple[Path, Path]] = field(default_factory=list)


@dataclass
class SyncProfile:
    """Active model/role variant for manifest filtering."""
    model: str | None = None
    role: str | None = None


def parse_sync_profile(path: Path) -> SyncProfile:
    """Parse a .sync-profile.yml file (simple key: value format)."""
    if not path.is_file():
        return SyncProfile()

    data: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            data[key.strip()] = value.strip()

    return SyncProfile(
        model=data.get("model") or None,
        role=data.get("role") or None,
    )


# ---------------------------------------------------------------------------
# FileClassifier — agent-specific file classification
# ---------------------------------------------------------------------------

class FileClassifier:
    """Classify vault files by agent ownership."""

    def __init__(self, agents: list[str]) -> None:
        self._agents = agents

    def classify(
        self, filepath: Path, base_path: Path, agent: str,
    ) -> tuple[Classification, Path]:
        """Classify a file for a given agent.

        Returns (classification, dest_relative_path).
        """
        rel = filepath.relative_to(base_path)

        if filepath.name in EXCLUDE_FILENAMES:
            return ("SKIP", rel)

        # Agent-specific directory (skills.copilot/, setup.claude/)
        for part in rel.parts[:-1]:
            for a in self._agents:
                if part.endswith(f".{a}"):
                    if a == agent:
                        return ("AGENT_DIR", self._remap_agent_dir(rel, a))
                    return ("SKIP", rel)

        # Nested override pattern: name.agent.ext
        for a in self._agents:
            if re.search(rf"\.{re.escape(a)}\.", filepath.name, re.IGNORECASE):
                if a == agent:
                    stripped = re.sub(rf"\.{re.escape(a)}\.", ".", filepath.name)
                    return ("AGENT_NESTED", rel.parent / stripped)
                return ("SKIP", rel)

        # Agent name as word segment in filename
        for a in self._agents:
            if re.search(rf"(^|[-_.]){re.escape(a)}([-_.]|$)", filepath.name, re.IGNORECASE):
                if a == agent:
                    return ("AGENT_NAMED", rel)
                return ("SKIP", rel)

        return ("UNIVERSAL", rel)

    def classify_with_dest(
        self, source_abs: Path, dest_rel: Path, agent: str,
    ) -> tuple[Classification, Path]:
        """Classify a file using its destination-relative path."""
        filename = source_abs.name

        if filename in EXCLUDE_FILENAMES:
            return ("SKIP", dest_rel)

        for part in dest_rel.parts[:-1]:
            for a in self._agents:
                if part.endswith(f".{a}"):
                    if a == agent:
                        return ("AGENT_DIR", self._remap_agent_dir(dest_rel, a))
                    return ("SKIP", dest_rel)

        for a in self._agents:
            if re.search(rf"\.{re.escape(a)}\.", filename, re.IGNORECASE):
                if a == agent:
                    stripped = re.sub(rf"\.{re.escape(a)}\.", ".", filename)
                    return ("AGENT_NESTED", dest_rel.parent / stripped)
                return ("SKIP", dest_rel)

        for a in self._agents:
            if re.search(rf"(^|[-_.]){re.escape(a)}([-_.]|$)", filename, re.IGNORECASE):
                if a == agent:
                    return ("AGENT_NAMED", dest_rel)
                return ("SKIP", dest_rel)

        return ("UNIVERSAL", dest_rel)

    def _remap_agent_dir(self, rel: Path, agent: str) -> Path:
        """Remap path inside agent directory to canonical location.

        skills/skills.copilot/python.md  →  skills/python.md
        instructions/setup.copilot/x.md  →  instructions/setup/x.md
        """
        new_parts: list[str] = []
        for part in rel.parts:
            if part.endswith(f".{agent}"):
                base = part.removesuffix(f".{agent}")
                if new_parts and new_parts[-1] == base:
                    continue
                new_parts.append(base)
            else:
                new_parts.append(part)
        return Path(*new_parts) if new_parts else rel


# ---------------------------------------------------------------------------
# TargetDiscovery — recursive directory-matching discovery
# ---------------------------------------------------------------------------

class TargetDiscovery:
    """Walk vault tree and discover sync targets by matching against repo dirs."""

    def discover(
        self, vault_dir: Path, repo_dir: Path,
        repo_root: Path | None = None,
    ) -> list[SyncTarget]:
        """Recursively discover where to create agent dirs."""
        if not vault_dir.exists():
            return []
        if repo_root is None:
            repo_root = repo_dir

        target = SyncTarget(repo_path=repo_dir)
        all_targets: list[SyncTarget] = [target]
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
        for entry in sorted(vault_dir.iterdir()):
            if entry.name.startswith("."):
                continue

            if entry.is_file():
                dest_rel = (
                    content_prefix / entry.name
                    if content_prefix != Path()
                    else Path(entry.name)
                )
                current_target.files.append((entry, dest_rel))

            elif entry.is_dir():
                if content_depth <= 1:
                    real_counterpart = current_target.repo_path / entry.name
                    if real_counterpart.is_dir():
                        new_target = SyncTarget(repo_path=real_counterpart)
                        all_targets.append(new_target)
                        self._walk_vault(
                            entry, real_counterpart, repo_root,
                            new_target, all_targets, Path(), 0,
                        )
                        continue

                in_content = content_prefix != Path()
                new_prefix = content_prefix / entry.name if in_content else Path(entry.name)
                new_depth = content_depth + 1 if content_depth > 0 else 1
                self._walk_vault(
                    entry, repo_dir, repo_root,
                    current_target, all_targets, new_prefix, new_depth,
                )


# ---------------------------------------------------------------------------
# ManifestBuilder — build agent-filtered file manifests
# ---------------------------------------------------------------------------

class ManifestBuilder:
    """Build classified file manifests from discovered targets or vault content."""

    def __init__(self, classifier: FileClassifier) -> None:
        self._classifier = classifier

    def for_target(
        self, agent: str, target: SyncTarget,
        vault_repo_root: Path, repo_root: Path,
    ) -> dict[Path, Path]:
        """Build classified manifest for a discovered target.

        Returns {dest_relative: source_absolute}.
        """
        manifest: dict[Path, tuple[Path, int]] = {}

        for source_abs, dest_rel in target.files:
            cls, classified_dest = self._classifier.classify_with_dest(
                source_abs, dest_rel, agent,
            )
            if cls == "SKIP":
                continue
            pri = PRIORITY[cls]
            if classified_dest not in manifest or pri > manifest[classified_dest][1]:
                manifest[classified_dest] = (source_abs, pri)

        # Inherit root-level files for non-root targets
        if target.repo_path != repo_root and vault_repo_root.exists():
            for f in sorted(vault_repo_root.iterdir()):
                if not f.is_file():
                    continue
                cls, classified_dest = self._classifier.classify_with_dest(
                    f, Path(f.name), agent,
                )
                if cls == "SKIP":
                    continue
                pri = PRIORITY[cls]
                if classified_dest not in manifest:
                    manifest[classified_dest] = (f, pri)

        return {dest: src for dest, (src, _) in manifest.items()}

    def for_vault(self, agent: str, vault_path: Path) -> dict[Path, tuple[Path, int]]:
        """Build manifest from vault-wide content directories.

        Returns {dest_relative: (source_absolute, priority)}.
        """
        manifest: dict[Path, tuple[Path, int]] = {}

        for content_dir_name in CONTENT_DIRS:
            content_dir = vault_path / content_dir_name
            if not content_dir.is_dir():
                continue
            for filepath in content_dir.rglob("*"):
                if not filepath.is_file():
                    continue
                cls, dest_rel = self._classifier.classify(filepath, vault_path, agent)
                if cls == "SKIP":
                    continue
                pri = PRIORITY[cls]
                if dest_rel not in manifest or pri > manifest[dest_rel][1]:
                    manifest[dest_rel] = (filepath, pri)

        return manifest


# ---------------------------------------------------------------------------
# VariantSelector — model/role filtering on manifests
# ---------------------------------------------------------------------------

class VariantSelector:
    """Filter manifest entries by active model and role profile.

    Operates on the manifest dict *after* ManifestBuilder and *before*
    SymlinkManager.  Three operations:

    1. Model swap — ``setup.claude.opus.md`` replaces ``setup.claude.md``
       when model=opus; all non-matching model variants are stripped.
    2. Role filter — files under ``roles/`` are included only when their
       stem matches the active role; all others are dropped.
    3. Strip — any remaining unmatched variant files are removed.
    """

    def __init__(self, model: str | None = None, role: str | None = None) -> None:
        self._model = model.lower() if model else None
        self._role = role.lower() if role else None

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def role(self) -> str | None:
        return self._role

    # -- Public API ---------------------------------------------------------

    def select(self, manifest: dict[Path, Path]) -> dict[Path, Path]:
        """Return a filtered copy of *manifest* with variant rules applied."""
        result = self._apply_model_swaps(dict(manifest))
        result = self._apply_role_filter(result)
        return result

    # -- Model variants -----------------------------------------------------

    def _apply_model_swaps(self, manifest: dict[Path, Path]) -> dict[Path, Path]:
        """Handle model-variant files (``*.{model}.*`` pattern).

        When model=opus:
          - ``setup.claude.opus.md`` → dest becomes ``setup.claude.md``
            (replaces the base file if present)
          - ``setup.claude.sonnet.md`` → stripped entirely

        When no model is set, all model-variant files are stripped so only
        base files remain.
        """
        # First pass: identify model-variant entries and their base paths
        variants: dict[Path, dict[str, tuple[Path, Path]]] = {}  # base_dest → {model → (dest, src)}
        regular: dict[Path, Path] = {}

        for dest, source in manifest.items():
            model_name = self._detect_model_in_dest(dest)
            if model_name is not None:
                base_dest = self._strip_model_from_dest(dest, model_name)
                if base_dest not in variants:
                    variants[base_dest] = {}
                variants[base_dest][model_name] = (dest, source)
            else:
                regular[dest] = source

        # Second pass: resolve which variant wins
        result: dict[Path, Path] = {}
        for dest, source in regular.items():
            if dest in variants and self._model and self._model in variants[dest]:
                # Active model variant replaces this base file
                _, variant_source = variants[dest][self._model]
                result[dest] = variant_source
            elif dest in variants:
                # No matching model variant — keep base as-is
                result[dest] = source
            else:
                result[dest] = source

        # Include model variants that have NO base file equivalent
        for base_dest, model_map in variants.items():
            if base_dest in result:
                continue  # already handled above
            if self._model and self._model in model_map:
                _, variant_source = model_map[self._model]
                result[base_dest] = variant_source
            # else: no matching variant and no base → omit entirely

        return result

    def _detect_model_in_dest(self, dest: Path) -> str | None:
        """Check if dest filename contains a known model name as a dotted segment.

        ``setup.claude.opus.md`` → ``"opus"``
        ``setup.claude.md``      → ``None``
        """
        parts = dest.name.split(".")
        for part in parts:
            if part.lower() in KNOWN_MODELS:
                return part.lower()
        return None

    def _strip_model_from_dest(self, dest: Path, model_name: str) -> Path:
        """Remove the model segment from a dest path.

        ``skills/setup.claude.opus.md`` → ``skills/setup.claude.md``
        """
        name = dest.name
        # Replace .model. with . (e.g. "setup.claude.opus.md" → "setup.claude.md")
        stripped = re.sub(rf"\.{re.escape(model_name)}\.", ".", name, count=1, flags=re.IGNORECASE)
        if stripped == name:
            # model might be last before extension: "thing.opus.md" → "thing.md"
            stripped = re.sub(rf"\.{re.escape(model_name)}\b", "", name, count=1, flags=re.IGNORECASE)
        return dest.parent / stripped if dest.parent != Path() else Path(stripped)

    # -- Role filter --------------------------------------------------------

    def _apply_role_filter(self, manifest: dict[Path, Path]) -> dict[Path, Path]:
        """Include/exclude files under ``roles/`` based on active role.

        - If role is set: include only ``roles/{role}.md`` (or ``roles/{role}/...``)
        - If role is not set: strip ALL role files
        """
        result: dict[Path, Path] = {}
        for dest, source in manifest.items():
            if self._is_role_path(dest):
                if self._role and self._matches_role(dest):
                    result[dest] = source
                # else: drop non-matching or all role files when no role set
            else:
                result[dest] = source
        return result

    def _is_role_path(self, dest: Path) -> bool:
        """Check if dest is inside a ``roles/`` directory."""
        return "roles" in dest.parts

    def _matches_role(self, dest: Path) -> bool:
        """Check if a role path matches the active role.

        Matches: ``roles/reviewer.md``, ``roles/reviewer/checklist.md``
        """
        if not self._role:
            return False
        parts = list(dest.parts)
        try:
            idx = parts.index("roles")
        except ValueError:
            return False
        if idx + 1 >= len(parts):
            return False
        next_part = parts[idx + 1]
        # Either "reviewer.md" (stem match) or "reviewer/" (dir match)
        stem = Path(next_part).stem
        return stem.lower() == self._role


# ---------------------------------------------------------------------------
# SymlinkManager — filesystem operations (create/remove symlinks)
# ---------------------------------------------------------------------------

class SymlinkManager:
    """Create and remove per-file symlinks in agent target directories."""

    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    # -- Shared primitives -----------------------------------------------

    def apply_manifest(
        self, target_dir: Path, manifest: dict[Path, Path],
    ) -> tuple[int, int, set[Path]]:
        """Apply a file manifest as symlinks. Returns (new, skip, created)."""
        created: set[Path] = set()
        count_new = 0
        count_skip = 0

        for dest_rel, source in sorted(manifest.items()):
            dest = target_dir / dest_rel
            created.add(dest)

            if not self.dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)

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

        return count_new, count_skip, created

    def prune_stale(
        self, root_dir: Path, created: set[Path],
        restrict_to: list[str] | None = None,
    ) -> int:
        """Remove stale symlinks and empty dirs.

        restrict_to: only prune inside these subdirectory names (e.g.
        CONTENT_DIRS for system targets — avoids touching ~/.claude/settings).
        """
        count_stale = 0

        if restrict_to:
            dirs_to_prune = [
                root_dir / name for name in restrict_to
                if (root_dir / name).exists()
            ]
        elif root_dir.exists():
            dirs_to_prune = [root_dir]
        else:
            return 0

        for prune_dir in dirs_to_prune:
            for link in list(prune_dir.rglob("*")):
                if link.is_symlink() and link not in created:
                    count_stale += 1
                    if not self.dry_run:
                        link.unlink()

            for dirpath in sorted(
                (d for d in prune_dir.rglob("*") if d.is_dir()),
                reverse=True,
            ):
                try:
                    if not any(dirpath.iterdir()) and not self.dry_run:
                        dirpath.rmdir()
                except OSError:
                    pass

            if restrict_to:
                try:
                    if prune_dir.is_dir() and not any(prune_dir.iterdir()) and not self.dry_run:
                        prune_dir.rmdir()
                except OSError:
                    pass

        return count_stale

    # -- High-level operations -------------------------------------------

    def sync_target(
        self, agent: str, target: SyncTarget,
        manifest_builder: ManifestBuilder,
        vault_repo_root: Path, repo_root: Path,
        agent_target_name: str,
        selector: VariantSelector | None = None,
    ) -> None:
        """Create per-file symlinks in target.repo_path/<agent_target>/."""
        target_dir = target.repo_path / agent_target_name
        print(f"  [{agent}] -> {agent_target_name}/")

        if target_dir.is_symlink():
            print("    Removing old directory symlink")
            if not self.dry_run:
                target_dir.unlink()

        if not self.dry_run:
            target_dir.mkdir(exist_ok=True)

        manifest = manifest_builder.for_target(agent, target, vault_repo_root, repo_root)
        if selector is not None:
            manifest = selector.select(manifest)
        count_new, count_skip, created = self.apply_manifest(target_dir, manifest)
        count_stale = self.prune_stale(target_dir, created)

        print(
            f"    {count_new} created, {count_skip} unchanged, "
            f"{count_stale} stale removed ({len(manifest)} total files)"
        )

    def sync_system(
        self, agent: str, vault_path: Path,
        manifest_builder: ManifestBuilder,
        agent_target_name: str,
        selector: VariantSelector | None = None,
    ) -> None:
        """Sync vault-wide content to ~/{agent_target}/."""
        target_dir = Path.home() / agent_target_name
        print(f"  [{agent}] -> ~/{agent_target_name}/")

        vault_manifest = manifest_builder.for_vault(agent, vault_path)

        if not self.dry_run:
            target_dir.mkdir(exist_ok=True)

        simple: dict[Path, Path] = {
            dest: src for dest, (src, _) in vault_manifest.items()
        }
        if selector is not None:
            simple = selector.select(simple)
        count_new, count_skip, created = self.apply_manifest(target_dir, simple)
        count_stale = self.prune_stale(target_dir, created, restrict_to=CONTENT_DIRS)

        print(
            f"    {count_new} created, {count_skip} unchanged, "
            f"{count_stale} stale removed ({len(vault_manifest)} total files)"
        )

    def remove_links(
        self, repo_path: Path, agents: list[str], agent_target_fn: _AgentTargetFn,
    ) -> None:
        """Remove all agent target directories from a repo."""
        for agent in agents:
            target_name = agent_target_fn(agent)
            target_dir = repo_path / target_name

            if target_dir.is_symlink():
                print(f"  Removing directory symlink: {target_dir.name}")
                if not self.dry_run:
                    target_dir.unlink()
            elif target_dir.is_dir():
                count = self.prune_stale(target_dir, created=set())
                try:
                    if target_dir.is_dir() and not any(target_dir.iterdir()) and not self.dry_run:
                        target_dir.rmdir()
                except OSError:
                    pass
                print(f"  Removed {count} symlinks from {target_dir.name}/")

    def remove_system_links(
        self, agents: list[str], agent_target_fn: _AgentTargetFn,
    ) -> None:
        """Remove vault-managed symlinks from system-level agent dirs."""
        for agent in agents:
            target_name = agent_target_fn(agent)
            target_dir = Path.home() / target_name
            if not target_dir.is_dir():
                continue
            count = self.prune_stale(target_dir, created=set(), restrict_to=CONTENT_DIRS)
            if count > 0:
                print(f"  Removed {count} symlinks from ~/{target_name}/")


# ---------------------------------------------------------------------------
# VaultSync — thin orchestrator
# ---------------------------------------------------------------------------

class VaultSync:
    """Orchestrator: coordinates config, classification, discovery, and sync."""

    def __init__(self, vault_path: Path = VAULT_PATH, dry_run: bool = False) -> None:
        self.vault_path = vault_path
        self.repos_base = vault_path / "repos"
        self.agents_index = vault_path / "agents" / "INDEX.md"
        self.repos_index = self.repos_base / "INDEX.md"
        self.dry_run = dry_run

        self._agents_cache: list[str] | None = None
        self._classifier: FileClassifier | None = None
        self._manifest: ManifestBuilder | None = None
        self._discovery = TargetDiscovery()
        self._linker = SymlinkManager(dry_run=dry_run)
        self._global_profile = self._load_profile()
        self._selector = VariantSelector(
            model=self._global_profile.model,
            role=self._global_profile.role,
        )

    # -- Lazy init (classifier needs agents list) ------------------------

    def _get_classifier(self) -> FileClassifier:
        if self._classifier is None:
            self._classifier = FileClassifier(self.get_registered_agents())
            self._manifest = ManifestBuilder(self._classifier)
        return self._classifier

    def _get_manifest_builder(self) -> ManifestBuilder:
        self._get_classifier()
        assert self._manifest is not None
        return self._manifest

    # -- Config parsing --------------------------------------------------

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
        """Parse repos/INDEX.md for repo paths."""
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

    def _load_profile(self, repo_name: str | None = None) -> SyncProfile:
        """Load sync profile, optionally with per-repo override merged on top."""
        global_profile = parse_sync_profile(self.vault_path / SYNC_PROFILE_FILE)
        if repo_name is None:
            return global_profile
        repo_profile = parse_sync_profile(
            self.repos_base / repo_name / SYNC_PROFILE_FILE,
        )
        return SyncProfile(
            model=repo_profile.model or global_profile.model,
            role=repo_profile.role or global_profile.role,
        )

    def _get_selector(self, repo_name: str | None = None) -> VariantSelector:
        """Get VariantSelector for the given repo (or global default)."""
        if repo_name is None:
            return self._selector
        profile = self._load_profile(repo_name)
        if profile == self._global_profile:
            return self._selector
        return VariantSelector(model=profile.model, role=profile.role)

    # -- Delegates (preserve public API) ---------------------------------

    def classify_file(
        self, filepath: Path, base_path: Path, agent: str,
    ) -> tuple[Classification, Path]:
        return self._get_classifier().classify(filepath, base_path, agent)

    def discover_sync_targets(
        self, vault_dir: Path, repo_dir: Path,
        repo_root: Path | None = None,
    ) -> list[SyncTarget]:
        return self._discovery.discover(vault_dir, repo_dir, repo_root)

    def build_target_manifest(
        self, agent: str, target: SyncTarget,
        vault_repo_root: Path, repo_root: Path,
    ) -> dict[Path, Path]:
        return self._get_manifest_builder().for_target(
            agent, target, vault_repo_root, repo_root,
        )

    def build_vault_manifest(self, agent: str) -> dict[Path, tuple[Path, int]]:
        return self._get_manifest_builder().for_vault(agent, self.vault_path)

    # -- Sync operations -------------------------------------------------

    def sync_target(
        self, agent: str, target: SyncTarget,
        vault_repo_root: Path, repo_root: Path,
        selector: VariantSelector | None = None,
    ) -> None:
        sel = selector or self._selector
        self._linker.sync_target(
            agent, target, self._get_manifest_builder(),
            vault_repo_root, repo_root, self.get_agent_target(agent),
            selector=sel,
        )

    def sync_all(
        self, agent: str | None = None, repo: str | None = None,
    ) -> None:
        """Sync all (or specific) agents and repos."""
        agents = [agent] if agent else self.get_registered_agents()
        repos = self.get_managed_repos()

        if repo:
            repo_path = Path(os.path.expanduser(repo))
            repos = [r for r in repos if r == repo_path or r.name == repo_path.name]
            if not repos:
                repo_path = Path.home() / "git" / repo
                if repo_path.exists():
                    repos = [repo_path]

        if not repos:
            print("No matching repos found.")
        else:
            for repo_path in repos:
                vault_repo = self.repos_base / repo_path.name
                if not repo_path.exists():
                    print(f"  Repo not found: {repo_path}")
                    continue

                repo_selector = self._get_selector(repo_path.name)
                targets = self.discover_sync_targets(vault_repo, repo_path)
                for target in targets:
                    label = repo_path.name
                    if target.repo_path != repo_path:
                        rel = target.repo_path.relative_to(repo_path)
                        label = f"{repo_path.name}/{rel}"
                    print(f"Syncing {label}:")
                    for a in agents:
                        if a not in self.get_registered_agents():
                            print(f"  Agent '{a}' not registered, skipping")
                            continue
                        self.sync_target(
                            a, target, vault_repo, repo_path,
                            selector=repo_selector,
                        )

        print(f"\nSyncing system-level agent config:")
        self.sync_system(agent=agent)
        print("\nSync complete.")

    def sync_system(self, agent: str | None = None) -> None:
        """Sync vault-wide content to ~/{agent_target}/."""
        agents = [agent] if agent else self.get_registered_agents()
        for a in agents:
            if a not in self.get_registered_agents():
                print(f"  Agent '{a}' not registered, skipping")
                continue
            self._linker.sync_system(
                a, self.vault_path,
                self._get_manifest_builder(), self.get_agent_target(a),
                selector=self._selector,
            )

    # -- Cleanup ---------------------------------------------------------

    def remove_links(self, repo_path: Path) -> None:
        self._linker.remove_links(
            repo_path, self.get_registered_agents(), self.get_agent_target,
        )

    def remove_system_links(self) -> None:
        self._linker.remove_system_links(
            self.get_registered_agents(), self.get_agent_target,
        )

    def remove_all(self, repo: str | None = None) -> None:
        """Remove links from all repos + system-level agent dirs."""
        repos = self.get_managed_repos()
        if repo:
            repo_path = Path(os.path.expanduser(repo))
            repos = [r for r in repos if r == repo_path or r.name == repo_path.name]

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

        print("Cleaning system-level agent config:")
        self.remove_system_links()

    def merge_and_relink(
        self, target: SyncTarget, vault_repo_root: Path,
        repo_root: Path, agent: str | None = None,
    ) -> None:
        """Post-git-merge: merge new files into vault, then re-sync."""
        agents = [agent] if agent else self.get_registered_agents()

        for a in agents:
            target_name = self.get_agent_target(a)
            target_dir = target.repo_path / target_name

            if not target_dir.is_dir() or target_dir.is_symlink():
                continue

            real_files = [
                f for f in target_dir.rglob("*")
                if f.is_file() and not f.is_symlink()
            ]
            if not real_files:
                continue

            print(f"  [{a}] Found {len(real_files)} real files in {target_name}/")
            print("    Merging new files into vault...")

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

            print(f"    Removing real {target_name}/ directory")
            if not self.dry_run:
                shutil.rmtree(target_dir)

            print("    Re-syncing...")
            self.sync_target(a, target, vault_repo_root, repo_root)


# ---------------------------------------------------------------------------
# File watcher
# ---------------------------------------------------------------------------

LOCK_FILE = Path("/tmp/obscura-watcher.pid")
DEBOUNCE_SECONDS = 0.5
FSWATCH_EXCLUDES: list[str] = [r"\.git", r"\.DS_Store", r"node_modules", r"__pycache__"]


class VaultWatcher:
    """Watch vault directories and re-sync on changes (requires fswatch)."""

    def __init__(
        self, vault_path: Path, sync: VaultSync,
        agent: str | None = None, repo: str | None = None,
    ) -> None:
        self.vault_path = vault_path
        self.sync = sync
        self.agent = agent
        self.repo = repo
        self._process: subprocess.Popen[str] | None = None
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
                os.kill(old_pid, 0)
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
        for entry in sorted(self.vault_path.iterdir()):
            if entry.is_dir() and "." in entry.name:
                base, _, _ = entry.name.partition(".")
                if base in CONTENT_DIRS:
                    paths.append(entry)
        # Watch the sync profile file for model/role changes
        profile_file = self.vault_path / SYNC_PROFILE_FILE
        if profile_file.is_file():
            paths.append(profile_file)
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
        # Reload profile so .sync-profile.yml changes take effect at runtime
        self.sync._global_profile = self.sync._load_profile()
        self.sync._selector = VariantSelector(
            model=self.sync._global_profile.model,
            role=self.sync._global_profile.role,
        )
        print("Re-syncing...")
        try:
            self.sync.sync_all(agent=self.agent, repo=self.repo)
        except Exception as e:
            print(f"Sync error: {e}", file=sys.stderr)

    def _cleanup(self, signum: int = 0, frame: types.FrameType | None = None) -> None:
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
        "--mode", choices=["symlink"], default="symlink",
        help="Sync mode (default: symlink)",
    )
    parser.add_argument("--agent", help="Specific agent to sync")
    parser.add_argument("--repo", help="Specific repo (name or path)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without changes")
    parser.add_argument("--clean", action="store_true", help="Remove all agent directories")
    parser.add_argument(
        "--merge", action="store_true",
        help="Merge real dirs into vault and re-sync (post-git-merge)",
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="Watch vault for changes and auto-sync (requires fswatch)",
    )

    args = parser.parse_args()
    vs = VaultSync(dry_run=args.dry_run)

    if args.dry_run:
        print("DRY RUN - no changes will be made\n")

    if args.watch:
        VaultWatcher(vs.vault_path, vs, agent=args.agent, repo=args.repo).run()
    elif args.clean:
        vs.remove_all(repo=args.repo)
    elif args.merge:
        repos = vs.get_managed_repos()
        if args.repo:
            rp = Path(os.path.expanduser(args.repo))
            repos = [r for r in repos if r == rp or r.name == rp.name]
        for repo_path in repos:
            if not repo_path.exists():
                continue
            vault_repo = vs.repos_base / repo_path.name
            for target in vs.discover_sync_targets(vault_repo, repo_path):
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
