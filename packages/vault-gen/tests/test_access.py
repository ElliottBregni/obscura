from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vault_gen.access.repo import RepoAccess

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git_setup(path: Path) -> None:
    """Run basic git init + user config in a directory."""
    for cmd in [
        ["git", "init"],
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
    ]:
        subprocess.run(cmd, cwd=path, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Minimal git repo with one commit — no vault-gen meta (open read/write)."""
    _git_setup(tmp_path)
    (tmp_path / "README.md").write_text("# Test repo\n\nHello from vault-gen tests.")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


@pytest.fixture
def config_repo(tmp_path: Path) -> Path:
    """Git repo with .vault-gen/meta.toml marking it as a config repo."""
    _git_setup(tmp_path)
    (tmp_path / "README.md").write_text("# Config repo")
    meta_dir = tmp_path / ".vault-gen"
    meta_dir.mkdir()
    (meta_dir / "meta.toml").write_text(
        '[vault]\nname = "test-config"\ntype = "config"\n'
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# TestInit
# ---------------------------------------------------------------------------


class TestInit:
    def test_accepts_git_repo(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        assert access.root == git_repo

    def test_accepts_string_path(self, git_repo: Path) -> None:
        access = RepoAccess(str(git_repo))
        assert access.root == git_repo

    def test_rejects_non_git_dir(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Not a git repository"):
            RepoAccess(tmp_path)

    def test_privilege_stored(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo, privilege="admin")
        assert access._privilege == "admin"

    def test_no_privilege_by_default(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        assert access._privilege is None


# ---------------------------------------------------------------------------
# TestRead
# ---------------------------------------------------------------------------


class TestRead:
    def test_reads_existing_file(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        content = access.read("README.md")
        assert "Test repo" in content

    def test_raises_on_missing_file(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        with pytest.raises(FileNotFoundError):
            access.read("nonexistent.md")

    def test_config_repo_read_requires_no_privilege(self, config_repo: Path) -> None:
        """Reads on config repos are always allowed."""
        access = RepoAccess(config_repo)  # no privilege
        assert "Config repo" in access.read("README.md")


# ---------------------------------------------------------------------------
# TestWrite
# ---------------------------------------------------------------------------


class TestWrite:
    def test_creates_file(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.write("notes/hello.md", "# Hello")
        assert (git_repo / "notes" / "hello.md").read_text() == "# Hello"

    def test_creates_parent_directories(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.write("deep/nested/dir/file.md", "content")
        assert (git_repo / "deep" / "nested" / "dir" / "file.md").exists()

    def test_no_commit_by_default(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.write("uncommitted.md", "content")
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        assert "uncommitted.md" in result.stdout

    def test_auto_commits_when_msg_given(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.write("committed.md", "# Content", commit_msg="feat: add note")
        log = access.history(n=1)
        assert log[0]["subject"] == "feat: add note"


# ---------------------------------------------------------------------------
# TestSearch
# ---------------------------------------------------------------------------


class TestSearch:
    def test_finds_match(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        results = access.search("vault-gen")
        assert len(results) > 0
        assert all("file" in r and "line" in r and "text" in r for r in results)

    def test_case_insensitive(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        upper = access.search("VAULT-GEN")
        lower = access.search("vault-gen")
        assert len(upper) == len(lower)

    def test_no_results_for_unknown_term(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        assert access.search("xyzzy_not_in_any_file") == []

    def test_respects_glob_pattern(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        (git_repo / "notes.txt").write_text("vault-gen mention")
        md_results = access.search("vault-gen mention", glob_pattern="**/*.md")
        assert all(r["file"].endswith(".md") for r in md_results)


# ---------------------------------------------------------------------------
# TestHistory
# ---------------------------------------------------------------------------


class TestHistory:
    def test_returns_initial_commit(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        log = access.history()
        assert len(log) >= 1
        assert log[0]["subject"] == "init"

    def test_entry_has_expected_keys(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        entry = access.history(n=1)[0]
        assert {"hash", "author", "date", "subject"} == set(entry.keys())

    def test_n_limits_results(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.write("a.md", "a", commit_msg="second")
        access.write("b.md", "b", commit_msg="third")
        assert len(access.history(n=2)) == 2
        assert len(access.history(n=10)) == 3

    def test_scoped_to_file(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.write("scoped.md", "content", commit_msg="scoped commit")
        log = access.history(path="scoped.md", n=10)
        assert len(log) == 1
        assert log[0]["subject"] == "scoped commit"


# ---------------------------------------------------------------------------
# TestDiff
# ---------------------------------------------------------------------------


class TestDiff:
    def test_diff_between_refs(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.write("new.md", "new content", commit_msg="add new")
        diff = access.diff("HEAD~1", "HEAD")
        assert "new.md" in diff
        assert "new content" in diff

    def test_empty_diff_for_same_ref(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        assert access.diff("HEAD", "HEAD") == ""


# ---------------------------------------------------------------------------
# TestListFiles
# ---------------------------------------------------------------------------


class TestListFiles:
    def test_returns_relative_paths(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        files = access.list_files("**/*.md")
        assert all(not Path(f).is_absolute() for f in files)

    def test_excludes_git_dir(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        files = access.list_files("**/*")
        assert not any(".git" in f for f in files)

    def test_glob_pattern_respected(self, git_repo: Path) -> None:
        (git_repo / "data.json").write_text("{}")
        access = RepoAccess(git_repo)
        md_files = access.list_files("**/*.md")
        json_files = access.list_files("**/*.json")
        assert all(f.endswith(".md") for f in md_files)
        assert all(f.endswith(".json") for f in json_files)


# ---------------------------------------------------------------------------
# TestSync
# ---------------------------------------------------------------------------


class TestSync:
    def test_returns_error_when_no_remote(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        result = access.sync()
        assert "error" in result
        assert "no remote" in result["error"]


# ---------------------------------------------------------------------------
# TestVersions
# ---------------------------------------------------------------------------


class TestVersions:
    def test_returns_all_commits(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.write("a.md", "v1", commit_msg="first")
        access.write("b.md", "v2", commit_msg="second")
        access.write("c.md", "v3", commit_msg="third")
        # history(n=2) would cap at 2; versions() should return all 4
        assert len(access.versions()) == 4

    def test_entry_shape(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        entry = access.versions()[0]
        assert {"hash", "author", "date", "subject"} == set(entry.keys())

    def test_scoped_to_file(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.write("tracked.md", "v1", commit_msg="tracked first")
        access.write("other.md", "x", commit_msg="unrelated")
        access.write("tracked.md", "v2", commit_msg="tracked second")

        all_versions = access.versions("tracked.md")
        assert len(all_versions) == 2
        assert all_versions[0]["subject"] == "tracked second"
        assert all_versions[1]["subject"] == "tracked first"

    def test_empty_path_returns_all(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        all_v = access.versions()
        none_v = access.versions(None)
        assert all_v == none_v


# ---------------------------------------------------------------------------
# TestRollback
# ---------------------------------------------------------------------------


class TestRollback:
    def test_restores_file_to_previous_state(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.write("note.md", "version 1", commit_msg="v1")
        access.write("note.md", "version 2", commit_msg="v2")

        # Roll back to v1 (HEAD~1 relative to current HEAD)
        changed = access.rollback("note.md", "HEAD~1")
        assert changed is True
        assert (git_repo / "note.md").read_text() == "version 1"

    def test_creates_rollback_commit(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.write("note.md", "v1", commit_msg="v1")
        access.write("note.md", "v2", commit_msg="v2")
        access.rollback("note.md", "HEAD~1")

        log = access.history(n=1)
        assert "revert" in log[0]["subject"]
        assert "note.md" in log[0]["subject"]

    def test_noop_when_already_at_that_state(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.write("note.md", "only version", commit_msg="only")
        # Rolling back to HEAD should be a no-op (file is already at that state)
        changed = access.rollback("note.md", "HEAD")
        assert changed is False

    def test_rollback_by_hash(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.write("note.md", "original", commit_msg="add note")
        original_hash = access.history(n=1)[0]["hash"]
        access.write("note.md", "changed", commit_msg="change note")

        access.rollback("note.md", original_hash)
        assert (git_repo / "note.md").read_text() == "original"


# ---------------------------------------------------------------------------
# TestRollbackRepo
# ---------------------------------------------------------------------------


class TestRollbackRepo:
    def test_resets_to_earlier_state(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.write("a.md", "content", commit_msg="add a")
        # Capture the state before this commit
        pre_ref = access.history(n=1)[0]["hash"]
        access.write("b.md", "will be gone", commit_msg="add b")

        assert (git_repo / "b.md").exists()
        access.rollback_repo(pre_ref)
        assert not (git_repo / "b.md").exists()

    def test_head_points_to_ref_after_reset(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        init_hash = access.history(n=1)[0]["hash"]
        access.write("extra.md", "x", commit_msg="extra")
        access.rollback_repo(init_hash)
        assert access.history(n=1)[0]["hash"] == init_hash


# ---------------------------------------------------------------------------
# TestSnapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_creates_lightweight_tag(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.snapshot("v1.0")
        result = subprocess.run(
            ["git", "tag", "-l"], cwd=git_repo, capture_output=True, text=True
        )
        assert "v1.0" in result.stdout

    def test_creates_annotated_tag_with_message(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.snapshot("v2.0", message="stable release")
        result = subprocess.run(
            ["git", "tag", "-v", "v2.0"], cwd=git_repo, capture_output=True, text=True
        )
        # git tag -v emits the tag object body to stdout (message is in stdout)
        assert "stable release" in result.stdout

    def test_tag_references_current_head(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.write("note.md", "content", commit_msg="add note")
        access.snapshot("checkpoint")
        head = access.history(n=1)[0]["hash"]
        result = subprocess.run(
            ["git", "rev-parse", "checkpoint"],
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == head

    def test_can_rollback_to_snapshot(self, git_repo: Path) -> None:
        access = RepoAccess(git_repo)
        access.write("a.md", "stable", commit_msg="stable state")
        access.snapshot("stable-point")
        access.write("b.md", "unstable", commit_msg="breaking change")

        access.rollback_repo("stable-point")
        assert not (git_repo / "b.md").exists()


# ---------------------------------------------------------------------------
# TestPermissions
# ---------------------------------------------------------------------------


class TestPermissions:
    # --- Config repo without privilege ---

    def test_config_repo_write_blocked_without_privilege(
        self, config_repo: Path
    ) -> None:
        access = RepoAccess(config_repo)
        with pytest.raises(PermissionError, match="privilege='admin'"):
            access.write("agents/new.md", "content")

    def test_config_repo_rollback_blocked_without_privilege(
        self, config_repo: Path
    ) -> None:
        access = RepoAccess(config_repo)
        with pytest.raises(PermissionError):
            access.rollback("README.md", "HEAD")

    def test_config_repo_rollback_repo_blocked_without_privilege(
        self, config_repo: Path
    ) -> None:
        access = RepoAccess(config_repo)
        with pytest.raises(PermissionError):
            access.rollback_repo("HEAD")

    def test_config_repo_snapshot_blocked_without_privilege(
        self, config_repo: Path
    ) -> None:
        access = RepoAccess(config_repo)
        with pytest.raises(PermissionError):
            access.snapshot("v1.0")

    def test_config_repo_read_always_allowed(self, config_repo: Path) -> None:
        access = RepoAccess(config_repo)
        content = access.read("README.md")
        assert content  # no exception

    def test_config_repo_search_always_allowed(self, config_repo: Path) -> None:
        access = RepoAccess(config_repo)
        results = access.search("Config")
        assert isinstance(results, list)

    def test_config_repo_history_always_allowed(self, config_repo: Path) -> None:
        access = RepoAccess(config_repo)
        log = access.history()
        assert len(log) >= 1

    def test_config_repo_versions_always_allowed(self, config_repo: Path) -> None:
        access = RepoAccess(config_repo)
        v = access.versions()
        assert len(v) >= 1

    # --- Config repo WITH privilege ---

    def test_config_repo_write_allowed_with_admin(self, config_repo: Path) -> None:
        access = RepoAccess(config_repo, privilege="admin")
        access.write("agents/new.md", "content", commit_msg="feat: add agent")
        assert (config_repo / "agents" / "new.md").exists()

    def test_config_repo_snapshot_allowed_with_admin(self, config_repo: Path) -> None:
        access = RepoAccess(config_repo, privilege="admin")
        access.snapshot("config-v1")
        result = subprocess.run(
            ["git", "tag", "-l"], cwd=config_repo, capture_output=True, text=True
        )
        assert "config-v1" in result.stdout

    # --- Vault repos are open ---

    def test_vault_repo_write_open(self, git_repo: Path) -> None:
        """Repos without a meta.toml (or type != config) have no restrictions."""
        access = RepoAccess(git_repo)
        access.write("note.md", "content")  # should not raise

    def test_vault_type_meta_is_open(self, tmp_path: Path) -> None:
        """A repo with type=vault in meta.toml should be unrestricted."""
        _git_setup(tmp_path)
        (tmp_path / "README.md").write_text("# Vault")
        meta_dir = tmp_path / ".vault-gen"
        meta_dir.mkdir()
        (meta_dir / "meta.toml").write_text(
            '[vault]\nname = "test-vault"\ntype = "vault"\n'
        )
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True
        )

        access = RepoAccess(tmp_path)  # no privilege needed
        access.write("Agents/output.md", "output")  # should not raise

    # --- Error message quality ---

    def test_permission_error_message_is_actionable(self, config_repo: Path) -> None:
        access = RepoAccess(config_repo)
        with pytest.raises(PermissionError) as exc_info:
            access.write("test.md", "content")
        msg = str(exc_info.value)
        assert "privilege='admin'" in msg
        assert "config" in msg.lower()
