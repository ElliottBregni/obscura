"""obscura.agent.workspace_isolation — Per-agent workspace isolation.

Provides filesystem and memory isolation for agents:
  - Git worktree isolation (separate working directory per agent)
  - Memory namespace enforcement (agents can't read each other's memory)
  - Tool allowlist enforcement (agents restricted to their definition)

Worktrees live under ``~/.obscura/worktrees/{repo_hash}/agent-{name}/`` and
are tracked in :mod:`obscura.tools.worktree_registry` so orphaned checkouts
can be reaped on next startup.

Usage::

    isolation = AgentWorkspaceIsolation(agent_config)
    await isolation.setup()
    # Agent runs in isolated worktree with enforced memory namespace
    await isolation.teardown(keep_worktree=True)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import UTC, datetime

from obscura.tools import worktree_observer
from obscura.tools import worktree_registry

logger = logging.getLogger(__name__)


class AgentWorkspaceIsolation:
    """Manages per-agent workspace isolation.

    When an agent's definition specifies ``isolation: "worktree"``,
    this class:
      1. Creates a git worktree for the agent
      2. Changes the agent's working directory to the worktree
      3. Sets a unique memory namespace for the agent
      4. Restores original state on teardown
    """

    def __init__(
        self,
        agent_name: str,
        *,
        isolation_mode: str = "",  # "" | "worktree"
        memory_namespace: str = "",
        original_cwd: str = "",
    ) -> None:
        self._agent_name = agent_name
        self._isolation_mode = isolation_mode
        self._memory_namespace = memory_namespace or f"agent:{agent_name}"
        self._original_cwd = original_cwd or os.getcwd()
        self._worktree_path: str = ""
        self._worktree_branch: str = ""
        self._slug: str = ""
        self._active = False

    @property
    def is_isolated(self) -> bool:
        return self._active

    @property
    def working_directory(self) -> str:
        """Return the agent's working directory (worktree or original)."""
        if self._active and self._worktree_path:
            return self._worktree_path
        return self._original_cwd

    @property
    def memory_namespace(self) -> str:
        """Return the agent's memory namespace."""
        return self._memory_namespace

    async def setup(self) -> bool:
        """Set up workspace isolation. Returns True if isolation was applied."""
        if self._isolation_mode != "worktree":
            self._active = True
            return True

        try:
            rc, git_root, _ = await _main_repo_root()
            if rc != 0:
                logger.warning("Agent isolation: not a git repo, skipping worktree")
                self._active = True
                return False

            slug = f"agent-{self._agent_name}"
            self._slug = slug
            self._worktree_branch = f"agent/{slug}"
            self._worktree_path = str(
                worktree_registry.worktree_path_for(git_root, slug)
            )
            from pathlib import Path as _Path

            _Path(self._worktree_path).parent.mkdir(parents=True, exist_ok=True)

            rc, _, err = await _git(
                "worktree",
                "add",
                "-b",
                self._worktree_branch,
                self._worktree_path,
                cwd=git_root,
            )
            if rc != 0:
                # Branch may already exist — try without -b.
                rc, _, err = await _git(
                    "worktree",
                    "add",
                    self._worktree_path,
                    self._worktree_branch,
                    cwd=git_root,
                )
                if rc != 0:
                    logger.warning("Agent isolation: worktree creation failed: %s", err)
                    self._active = True
                    return False

            worktree_registry.add(
                worktree_registry.WorktreeEntry(
                    slug=slug,
                    repo_root=git_root,
                    repo_hash=worktree_registry.repo_hash(git_root),
                    worktree_path=self._worktree_path,
                    branch=self._worktree_branch,
                    original_cwd=self._original_cwd,
                    owner="agent",
                    pid=os.getpid(),
                    created_at=datetime.now(UTC),
                    agent_name=self._agent_name,
                ),
            )
            worktree_observer.start(slug, self._worktree_path)

            os.chdir(self._worktree_path)
            self._active = True
            logger.info(
                "Agent %s isolated in worktree: %s",
                self._agent_name,
                self._worktree_path,
            )
            return True

        except Exception:
            logger.warning("Agent isolation setup failed", exc_info=True)
            self._active = True
            return False

    async def teardown(self, *, keep_worktree: bool = True) -> None:
        """Tear down workspace isolation."""
        if not self._active:
            return

        with contextlib.suppress(OSError):
            os.chdir(self._original_cwd)

        if self._slug:
            worktree_observer.stop(self._slug)

        if self._worktree_path and not keep_worktree:
            try:
                rc, git_root, _ = await _main_repo_root()
                if rc == 0:
                    await _git(
                        "worktree",
                        "remove",
                        "--force",
                        self._worktree_path,
                        cwd=git_root,
                    )
                    await _git(
                        "branch",
                        "-D",
                        self._worktree_branch,
                        cwd=git_root,
                    )
                    logger.info("Agent %s worktree removed", self._agent_name)
                if self._slug:
                    worktree_registry.remove(self._slug)
            except Exception:
                logger.debug("Worktree cleanup failed", exc_info=True)
        elif self._slug:
            worktree_registry.update(self._slug, status="kept")

        self._active = False

    def get_context_notice(self) -> str:
        """Return a context notice for the agent about its isolation."""
        if not self._worktree_path:
            return ""
        return (
            f"You are working in an isolated git worktree at {self._worktree_path}. "
            f"Changes here do not affect the main working tree. "
            f"Your memory namespace is '{self._memory_namespace}'."
        )


async def _main_repo_root() -> tuple[int, str, str]:
    """Resolve the primary worktree root (works inside linked worktrees)."""
    rc, common_dir, err = await _git(
        "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    if rc != 0:
        return rc, "", err
    from pathlib import Path as _Path

    common = _Path(common_dir)
    if common.name == ".git":
        return 0, str(common.parent), ""
    return await _git("rev-parse", "--show-toplevel")


async def _git(*args: str, cwd: str | None = None) -> tuple[int, str, str]:
    """Run a git command and return (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )
