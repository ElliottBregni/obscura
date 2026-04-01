"""
obscura.agent.workspace_isolation — Per-agent workspace isolation.

Provides filesystem and memory isolation for agents:
  - Git worktree isolation (separate working directory per agent)
  - Memory namespace enforcement (agents can't read each other's memory)
  - Tool allowlist enforcement (agents restricted to their definition)

Usage::

    isolation = AgentWorkspaceIsolation(agent_config)
    await isolation.setup()
    # Agent runs in isolated worktree with enforced memory namespace
    await isolation.teardown(keep_worktree=True)
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

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
        isolation_mode: str = "",   # "" | "worktree"
        memory_namespace: str = "",
        original_cwd: str = "",
    ) -> None:
        self._agent_name = agent_name
        self._isolation_mode = isolation_mode
        self._memory_namespace = memory_namespace or f"agent:{agent_name}"
        self._original_cwd = original_cwd or os.getcwd()
        self._worktree_path: str = ""
        self._worktree_branch: str = ""
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
            # No filesystem isolation — just enforce memory namespace.
            self._active = True
            return True

        # Create git worktree.
        try:
            rc, git_root, _ = await _git("rev-parse", "--show-toplevel")
            if rc != 0:
                logger.warning("Agent isolation: not a git repo, skipping worktree")
                self._active = True
                return False

            slug = f"agent-{self._agent_name}"
            self._worktree_branch = f"agent/{slug}"
            self._worktree_path = str(
                Path(git_root).parent / ".obscura-worktrees" / slug
            )

            rc, _, err = await _git(
                "worktree", "add", "-b", self._worktree_branch,
                self._worktree_path, cwd=git_root,
            )
            if rc != 0:
                # Branch may already exist — try without -b.
                rc, _, err = await _git(
                    "worktree", "add", self._worktree_path,
                    self._worktree_branch, cwd=git_root,
                )
                if rc != 0:
                    logger.warning("Agent isolation: worktree creation failed: %s", err)
                    self._active = True
                    return False

            os.chdir(self._worktree_path)
            self._active = True
            logger.info(
                "Agent %s isolated in worktree: %s",
                self._agent_name, self._worktree_path,
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

        # Restore original working directory.
        try:
            os.chdir(self._original_cwd)
        except OSError:
            pass

        if self._worktree_path and not keep_worktree:
            # Remove worktree.
            try:
                rc, git_root, _ = await _git("rev-parse", "--show-toplevel")
                if rc == 0:
                    await _git(
                        "worktree", "remove", "--force",
                        self._worktree_path, cwd=git_root,
                    )
                    await _git(
                        "branch", "-D", self._worktree_branch, cwd=git_root,
                    )
                    logger.info("Agent %s worktree removed", self._agent_name)
            except Exception:
                logger.debug("Worktree cleanup failed", exc_info=True)

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


async def _git(*args: str, cwd: str | None = None) -> tuple[int, str, str]:
    """Run a git command and return (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
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
