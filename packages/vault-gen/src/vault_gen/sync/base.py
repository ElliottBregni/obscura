from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from vault_gen.access.repo import RepoAccess


@dataclass(frozen=True)
class Change:
    """A single change that would be applied by push, or was applied by pull."""

    path: str
    action: str  # "add" | "update" | "remove"
    detail: str = ""


@dataclass(frozen=True)
class SyncResult:
    """Outcome of a push or pull operation."""

    success: bool
    adapter: str
    changes: tuple[Change, ...] = ()
    error: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )


class SyncAdapter(ABC):
    """Base class for sync adapters that bridge config repos to external backends.

    Adapters are discovered either via the ``vault_gen.sync_adapters`` entry
    point group (for third-party plugins) or are built-in to vault-gen.

    Each adapter reads its per-repo configuration from the ``sync.toml`` file
    at the repo root via the ``config`` dict passed to every method.

    All operations are async to accommodate adapters that make network calls.
    Use ``asyncio.run()`` (or the CLI wrappers) to invoke them from sync code.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique adapter identifier. Must match the ``name`` field in sync.toml."""

    @abstractmethod
    async def push(self, repo: RepoAccess, config: Mapping[str, object]) -> SyncResult:
        """Push repo state to the external backend.

        Should be idempotent: running push twice in a row with no intervening
        changes should result in zero changes on the second run.
        """

    @abstractmethod
    async def pull(self, repo: RepoAccess, config: Mapping[str, object]) -> SyncResult:
        """Pull current state from the external backend into the repo.

        Writes files and auto-commits via ``repo.write(..., commit_msg=...)``.
        """

    @abstractmethod
    async def diff(
        self, repo: RepoAccess, config: Mapping[str, object]
    ) -> list[Change]:
        """Return what would change on push without applying anything.

        Used by ``vault-gen sync push --dry-run`` and ``vault-gen sync diff``.
        """
