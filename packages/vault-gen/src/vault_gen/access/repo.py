"""RepoAccess — thin git + file I/O wrapper for vault-gen repos.

Stdlib-only. This file is copied verbatim as _access/repo.py into every
generated repo and must remain free of third-party dependencies.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path


class RepoAccess:
    """Programmatic read/write interface for a vault-gen repo.

    For config repos, all write operations require ``privilege="admin"`` to
    prevent accidental changes to fleet configuration:

        repo = RepoAccess("/path/to/config-repo", privilege="admin")

    Vault repos are open for read/write with no privilege requirement.

    Usage::

        from _access import RepoAccess

        repo = RepoAccess("/path/to/repo")
        content = repo.read("Agents/summary.md")
        repo.write("Memory/snapshot.md", content, commit_msg="mem: update snapshot")
        hits = repo.search("KAIROS")
        log = repo.history(n=10)
        log = repo.versions("Memory/snapshot.md")   # full history for one file
        repo.rollback("Memory/snapshot.md", "HEAD~2")
        repo.snapshot("v1.0", message="stable config snapshot")
        repo.sync()
    """

    def __init__(self, root: Path | str, privilege: str | None = None) -> None:
        self.root = Path(root).resolve()
        if not (self.root / ".git").exists():
            raise ValueError(f"Not a git repository: {self.root}")
        self._privilege = privilege
        self._meta: dict[str, object] = self._load_meta()

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def read(self, path: str | Path) -> str:
        """Read a file from the repo and return its contents."""
        return (self.root / path).read_text()

    def write(
        self,
        path: str | Path,
        content: str,
        commit_msg: str | None = None,
    ) -> None:
        """Write content to a file.

        Raises PermissionError for config repos unless privilege="admin".
        If commit_msg is provided the file is staged and committed immediately.
        Parent directories are created as needed.
        """
        self._check_write_permission()
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        if commit_msg:
            self._run("git", "add", str(target.relative_to(self.root)))
            self._run("git", "commit", "-m", commit_msg)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        glob_pattern: str = "**/*.md",
    ) -> list[dict[str, str]]:
        """Full-text search across files matching glob_pattern.

        Returns a list of matches, each a dict with keys:
            file   — path relative to repo root
            line   — 1-based line number (as str)
            text   — matched line content (stripped)
        """
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        results: list[dict[str, str]] = []
        for file_path in self.list_files(glob_pattern):
            try:
                lines = (self.root / file_path).read_text().splitlines()
            except OSError:
                continue
            for lineno, line in enumerate(lines, 1):
                if pattern.search(line):
                    results.append(
                        {"file": file_path, "line": str(lineno), "text": line.strip()}
                    )
        return results

    # ------------------------------------------------------------------
    # Git — log / diff / listing
    # ------------------------------------------------------------------

    def history(
        self,
        path: str | Path | None = None,
        n: int = 10,
    ) -> list[dict[str, str]]:
        """Return the n most recent git log entries.

        Each entry is a dict with keys: hash, author, date, subject.
        Pass path to scope the log to a specific file.
        """
        args = ["git", "log", f"-{n}", "--format=%H\x1f%an\x1f%ai\x1f%s"]
        if path:
            args += ["--", str(path)]
        return self._parse_log(self._run(*args))

    def versions(self, path: str | Path | None = None) -> list[dict[str, str]]:
        """Return the full version history — all commits, no page limit.

        Each entry is a dict with keys: hash, author, date, subject.
        Pass path to scope the log to commits that touched that file.

        This is the versioning API: use it to enumerate all snapshots of a
        file or the whole repo before calling rollback() or diff().
        """
        args = ["git", "log", "--format=%H\x1f%an\x1f%ai\x1f%s"]
        if path:
            args += ["--", str(path)]
        return self._parse_log(self._run(*args))

    def diff(self, ref1: str = "HEAD~1", ref2: str = "HEAD") -> str:
        """Return the unified diff between two git refs."""
        return self._run("git", "diff", ref1, ref2)

    def list_files(self, glob_pattern: str = "**/*.md") -> list[str]:
        """List files matching glob_pattern, relative to repo root.

        Excludes anything inside .git/.
        """
        return sorted(
            str(p.relative_to(self.root))
            for p in self.root.glob(glob_pattern)
            if p.is_file() and ".git" not in p.parts
        )

    # ------------------------------------------------------------------
    # Git — versioning operations (write; permission-gated for config repos)
    # ------------------------------------------------------------------

    def rollback(self, path: str | Path, ref: str) -> bool:
        """Restore a single file to its state at a given commit ref.

        Raises PermissionError for config repos unless privilege="admin".

        Returns True if the file was different (a new rollback commit was
        created), False if it was already at that state (no-op).
        """
        self._check_write_permission()
        rel = str(Path(path))
        self._run("git", "checkout", ref, "--", rel)

        # Only commit if the checkout actually changed something.
        status = self._run("git", "status", "--porcelain", rel).strip()
        if not status:
            return False

        short_ref = ref[:8] if len(ref) > 8 else ref
        self._run("git", "commit", "-m", f"revert({rel}): restore to {short_ref}")
        return True

    def rollback_repo(self, ref: str) -> None:
        """Reset the entire repo to a point in time via git reset --hard.

        Raises PermissionError for config repos unless privilege="admin".

        WARNING: This is destructive. Commits after ref become unreachable.
        Tag first with snapshot() if you want to preserve the current state.
        """
        self._check_write_permission()
        self._run("git", "reset", "--hard", ref)

    def snapshot(self, tag_name: str, message: str | None = None) -> None:
        """Tag the current HEAD for easy reference.

        Raises PermissionError for config repos unless privilege="admin".

        Creates a lightweight tag (no message) or an annotated tag (with message).
        Tags are the recommended way to mark stable points before rollback_repo().
        """
        self._check_write_permission()
        if message:
            self._run("git", "tag", "-a", tag_name, "-m", message)
        else:
            self._run("git", "tag", tag_name)

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------

    def sync(self) -> dict[str, str]:
        """Pull then push if a remote is configured.

        Returns a dict summarising what happened. Keys are 'pull', 'push',
        'pull_error', 'push_error', or 'error' (no remote).
        """
        remotes = self._run("git", "remote").strip()
        if not remotes:
            return {"error": "no remote configured"}

        result: dict[str, str] = {}
        try:
            result["pull"] = self._run("git", "pull", "--rebase").strip()
        except RuntimeError as exc:
            result["pull_error"] = str(exc)

        try:
            result["push"] = self._run("git", "push").strip()
        except RuntimeError as exc:
            result["push_error"] = str(exc)

        return result

    # ------------------------------------------------------------------
    # Obscura integration
    # ------------------------------------------------------------------

    def link_obsura(self, obscura_path: Path | str) -> None:
        """Register this repo in an Obscura instance's vault registry.

        Writes/updates <obscura_path>/config/vaults.toml with an entry for
        this repo. Creates the file if it does not exist.
        """
        op = Path(obscura_path)
        config_dir = op / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        vaults_file = config_dir / "vaults.toml"

        vaults: list[dict[str, str]] = []
        if vaults_file.exists():
            data = tomllib.loads(vaults_file.read_text())
            raw = data.get("vaults", [])
            vaults = [v for v in raw if isinstance(v, dict)]

        new_entry: dict[str, str] = {
            "name": self.root.name,
            "path": str(self.root),
            "type": "vault-gen",
        }

        idx = next(
            (i for i, v in enumerate(vaults) if v.get("path") == str(self.root)),
            None,
        )
        if idx is not None:
            vaults[idx] = new_entry
        else:
            vaults.append(new_entry)

        vaults_file.write_text(_format_toml_array_of_tables("vaults", vaults))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_meta(self) -> dict[str, object]:
        meta_file = self.root / ".vault-gen" / "meta.toml"
        if meta_file.exists():
            return tomllib.loads(meta_file.read_text())
        return {}

    def _repo_type(self) -> str | None:
        vault_section: object = self._meta.get("vault", {})
        if isinstance(vault_section, dict):
            val = vault_section.get("type")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            return str(val) if val is not None else None  # pyright: ignore[reportUnknownArgumentType]
        return None

    def _check_write_permission(self) -> None:
        if self._repo_type() == "config" and self._privilege != "admin":
            raise PermissionError(
                f"Writes to config repos require elevated privileges.\n"
                f"Use RepoAccess({str(self.root)!r}, privilege='admin') to enable writes.\n"
                f"Config repos are fleet configuration — "
                f"accidental writes can break running agents."
            )

    def _parse_log(self, output: str) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        for line in output.strip().splitlines():
            if not line:
                continue
            parts = line.split("\x1f", 3)
            if len(parts) == 4:
                entries.append(
                    {
                        "hash": parts[0],
                        "author": parts[1],
                        "date": parts[2],
                        "subject": parts[3],
                    }
                )
        return entries

    def _run(self, *args: str) -> str:
        result = subprocess.run(
            list(args),
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"`{' '.join(args)}` failed:\n{result.stderr.strip()}"
            )
        return result.stdout


def _format_toml_array_of_tables(key: str, items: list[dict[str, str]]) -> str:
    """Minimal TOML serialiser for an array of tables — no external deps."""
    lines = ["# vault-gen registry — do not edit by hand\n"]
    for item in items:
        lines.append(f"[[{key}]]")
        for k, v in item.items():
            escaped = v.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{k} = "{escaped}"')
        lines.append("")
    return "\n".join(lines)
