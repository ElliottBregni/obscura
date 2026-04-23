"""Tests for the Unleash sync adapter.

HTTP calls are intercepted by respx so no real Unleash instance is needed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx
import pytest
import respx

from vault_gen.access.repo import RepoAccess
from vault_gen.sync.adapters.unleash import (
    FlagSpec,
    UnleashAdapter,
    _flag_differs,
    _flag_to_toml,
    _read_flags,
    _require_token,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BASE = "http://unleash-test:4242"
_TOKEN = "test-admin-token"
_CONFIG = {
    "base_url": _BASE,
    "project": "default",
    "environment": "development",
    "flags_dir": "flags/",
}


def _git_setup(path: Path) -> None:
    for cmd in [
        ["git", "init"],
        ["git", "config", "user.email", "test@test.com"],
        ["git", "config", "user.name", "Test"],
    ]:
        subprocess.run(cmd, cwd=path, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    _git_setup(tmp_path)
    (tmp_path / "README.md").write_text("# test")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


@pytest.fixture
def repo(git_repo: Path) -> RepoAccess:
    return RepoAccess(git_repo)


@pytest.fixture(autouse=True)
def set_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_GEN_UNLEASH_TOKEN", _TOKEN)


def _features_response(*flags: dict) -> httpx.Response:
    return httpx.Response(200, json={"features": list(flags)})


def _feature_payload(name: str, **kwargs) -> dict:
    return {"name": name, "description": "", "type": "release", "enabled": True, **kwargs}


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------


class TestFlagToToml:
    def test_basic_serialisation(self) -> None:
        flag = FlagSpec(name="dark-mode", description="Dark mode", type="release", enabled=True)
        toml = _flag_to_toml(flag)
        assert 'name = "dark-mode"' in toml
        assert 'description = "Dark mode"' in toml
        assert 'type = "release"' in toml
        assert "enabled = true" in toml

    def test_disabled_flag(self) -> None:
        flag = FlagSpec(name="x", enabled=False)
        assert "enabled = false" in _flag_to_toml(flag)

    def test_strategies_included(self) -> None:
        flag = FlagSpec(name="x", strategies=[{"name": "default"}])
        toml = _flag_to_toml(flag)
        assert "[[strategies]]" in toml
        assert 'name = "default"' in toml

    def test_escapes_quotes_in_strings(self) -> None:
        flag = FlagSpec(name='x"y', description='has "quotes"')
        toml = _flag_to_toml(flag)
        assert '\\"' in toml

    def test_ends_with_newline(self) -> None:
        assert _flag_to_toml(FlagSpec(name="x")).endswith("\n")


class TestFlagDiffers:
    def test_identical_flags_do_not_differ(self) -> None:
        a = FlagSpec(name="x", description="d", type="release", enabled=True)
        b = FlagSpec(name="x", description="d", type="release", enabled=True)
        assert _flag_differs(a, b) is False

    def test_description_change_detected(self) -> None:
        a = FlagSpec(name="x", description="old")
        b = FlagSpec(name="x", description="new")
        assert _flag_differs(a, b) is True

    def test_type_change_detected(self) -> None:
        a = FlagSpec(name="x", type="release")
        b = FlagSpec(name="x", type="experiment")
        assert _flag_differs(a, b) is True

    def test_enabled_change_detected(self) -> None:
        a = FlagSpec(name="x", enabled=True)
        b = FlagSpec(name="x", enabled=False)
        assert _flag_differs(a, b) is True


class TestRequireToken:
    def test_returns_token_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VAULT_GEN_UNLEASH_TOKEN", "abc123")
        assert _require_token() == "abc123"

    def test_raises_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VAULT_GEN_UNLEASH_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="VAULT_GEN_UNLEASH_TOKEN"):
            _require_token()

    def test_raises_when_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VAULT_GEN_UNLEASH_TOKEN", "   ")
        with pytest.raises(RuntimeError):
            _require_token()


class TestReadFlags:
    def test_reads_toml_files(self, git_repo: Path) -> None:
        flags_dir = git_repo / "flags"
        flags_dir.mkdir()
        (flags_dir / "dark-mode.toml").write_text(
            'name = "dark-mode"\ndescription = ""\ntype = "release"\nenabled = true\n'
        )
        access = RepoAccess(git_repo)
        flags = _read_flags(access, "flags/")
        assert "dark-mode" in flags
        assert flags["dark-mode"].type == "release"

    def test_skips_unparseable_files(self, git_repo: Path, caplog) -> None:
        flags_dir = git_repo / "flags"
        flags_dir.mkdir()
        (flags_dir / "bad.toml").write_text("not valid toml ][")
        access = RepoAccess(git_repo)
        flags = _read_flags(access, "flags/")
        assert flags == {}

    def test_returns_empty_when_no_flags_dir(self, repo: RepoAccess) -> None:
        assert _read_flags(repo, "flags/") == {}

    def test_multiple_flags(self, git_repo: Path) -> None:
        flags_dir = git_repo / "flags"
        flags_dir.mkdir()
        for name in ("alpha", "beta", "gamma"):
            (flags_dir / f"{name}.toml").write_text(
                f'name = "{name}"\ntype = "release"\nenabled = true\n'
            )
        flags = _read_flags(RepoAccess(git_repo), "flags/")
        assert set(flags) == {"alpha", "beta", "gamma"}


# ---------------------------------------------------------------------------
# Adapter — diff
# ---------------------------------------------------------------------------


class TestUnleashAdapterDiff:
    @respx.mock
    async def test_new_flag_in_repo_shows_as_add(self, repo: RepoAccess) -> None:
        (repo.root / "flags").mkdir()
        (repo.root / "flags" / "new-flag.toml").write_text(
            'name = "new-flag"\ntype = "release"\nenabled = true\n'
        )
        respx.get(f"{_BASE}/api/admin/projects/default/features").mock(
            return_value=_features_response()  # empty remote
        )
        adapter = UnleashAdapter()
        changes = await adapter.diff(repo, _CONFIG)
        assert len(changes) == 1
        assert changes[0].action == "add"
        assert "new-flag" in changes[0].path

    @respx.mock
    async def test_flag_in_remote_not_in_repo_shows_as_remove(
        self, repo: RepoAccess
    ) -> None:
        respx.get(f"{_BASE}/api/admin/projects/default/features").mock(
            return_value=_features_response(_feature_payload("orphan"))
        )
        adapter = UnleashAdapter()
        changes = await adapter.diff(repo, _CONFIG)
        assert len(changes) == 1
        assert changes[0].action == "remove"
        assert "orphan" in changes[0].detail

    @respx.mock
    async def test_changed_flag_shows_as_update(self, repo: RepoAccess) -> None:
        (repo.root / "flags").mkdir()
        (repo.root / "flags" / "my-flag.toml").write_text(
            'name = "my-flag"\ndescription = "updated desc"\ntype = "release"\nenabled = true\n'
        )
        respx.get(f"{_BASE}/api/admin/projects/default/features").mock(
            return_value=_features_response(
                _feature_payload("my-flag", description="old desc")
            )
        )
        adapter = UnleashAdapter()
        changes = await adapter.diff(repo, _CONFIG)
        assert len(changes) == 1
        assert changes[0].action == "update"

    @respx.mock
    async def test_no_changes_when_in_sync(self, repo: RepoAccess) -> None:
        (repo.root / "flags").mkdir()
        (repo.root / "flags" / "stable.toml").write_text(
            'name = "stable"\ndescription = ""\ntype = "release"\nenabled = true\n'
        )
        respx.get(f"{_BASE}/api/admin/projects/default/features").mock(
            return_value=_features_response(_feature_payload("stable"))
        )
        adapter = UnleashAdapter()
        changes = await adapter.diff(repo, _CONFIG)
        assert changes == []


# ---------------------------------------------------------------------------
# Adapter — push
# ---------------------------------------------------------------------------


class TestUnleashAdapterPush:
    @respx.mock
    async def test_creates_new_flag(self, repo: RepoAccess) -> None:
        (repo.root / "flags").mkdir()
        (repo.root / "flags" / "my-flag.toml").write_text(
            'name = "my-flag"\ntype = "release"\nenabled = true\n'
        )
        respx.get(f"{_BASE}/api/admin/projects/default/features").mock(
            return_value=_features_response()
        )
        create_route = respx.post(
            f"{_BASE}/api/admin/projects/default/features"
        ).mock(return_value=httpx.Response(201, json={}))
        respx.post(
            f"{_BASE}/api/admin/projects/default/features/my-flag/environments/development/on"
        ).mock(return_value=httpx.Response(200, json={}))

        adapter = UnleashAdapter()
        result = await adapter.push(repo, _CONFIG)

        assert result.success is True
        assert len(result.changes) == 1
        assert result.changes[0].action == "add"
        assert create_route.called

    @respx.mock
    async def test_updates_changed_flag(self, repo: RepoAccess) -> None:
        (repo.root / "flags").mkdir()
        (repo.root / "flags" / "my-flag.toml").write_text(
            'name = "my-flag"\ndescription = "new"\ntype = "release"\nenabled = true\n'
        )
        respx.get(f"{_BASE}/api/admin/projects/default/features").mock(
            return_value=_features_response(_feature_payload("my-flag", description="old"))
        )
        update_route = respx.put(
            f"{_BASE}/api/admin/projects/default/features/my-flag"
        ).mock(return_value=httpx.Response(200, json={}))
        respx.post(
            f"{_BASE}/api/admin/projects/default/features/my-flag/environments/development/on"
        ).mock(return_value=httpx.Response(200, json={}))

        adapter = UnleashAdapter()
        result = await adapter.push(repo, _CONFIG)

        assert result.success is True
        assert result.changes[0].action == "update"
        assert update_route.called

    @respx.mock
    async def test_archives_removed_flag(self, repo: RepoAccess) -> None:
        # No flags in repo — remote has one -> should archive it.
        respx.get(f"{_BASE}/api/admin/projects/default/features").mock(
            return_value=_features_response(_feature_payload("stale-flag"))
        )
        archive_route = respx.delete(
            f"{_BASE}/api/admin/projects/default/features/stale-flag"
        ).mock(return_value=httpx.Response(200, json={}))

        adapter = UnleashAdapter()
        result = await adapter.push(repo, _CONFIG)

        assert result.success is True
        assert result.changes[0].action == "remove"
        assert archive_route.called

    @respx.mock
    async def test_returns_failure_on_http_error(self, repo: RepoAccess) -> None:
        (repo.root / "flags").mkdir()
        (repo.root / "flags" / "x.toml").write_text(
            'name = "x"\ntype = "release"\nenabled = true\n'
        )
        respx.get(f"{_BASE}/api/admin/projects/default/features").mock(
            return_value=_features_response()
        )
        respx.post(f"{_BASE}/api/admin/projects/default/features").mock(
            return_value=httpx.Response(403, json={"message": "Forbidden"})
        )

        adapter = UnleashAdapter()
        result = await adapter.push(repo, _CONFIG)

        assert result.success is False
        assert result.error is not None
        assert "403" in result.error

    @respx.mock
    async def test_no_changes_when_already_in_sync(self, repo: RepoAccess) -> None:
        (repo.root / "flags").mkdir()
        (repo.root / "flags" / "stable.toml").write_text(
            'name = "stable"\ndescription = ""\ntype = "release"\nenabled = true\n'
        )
        respx.get(f"{_BASE}/api/admin/projects/default/features").mock(
            return_value=_features_response(_feature_payload("stable"))
        )
        adapter = UnleashAdapter()
        result = await adapter.push(repo, _CONFIG)
        assert result.success is True
        assert result.changes == ()


# ---------------------------------------------------------------------------
# Adapter — pull
# ---------------------------------------------------------------------------


class TestUnleashAdapterPull:
    @respx.mock
    async def test_pulls_new_flags_from_remote(self, repo: RepoAccess) -> None:
        respx.get(f"{_BASE}/api/admin/projects/default/features").mock(
            return_value=_features_response(
                _feature_payload("dark-mode", description="Dark mode")
            )
        )
        adapter = UnleashAdapter()
        result = await adapter.pull(repo, _CONFIG)

        assert result.success is True
        assert len(result.changes) == 1
        assert result.changes[0].action == "add"
        assert (repo.root / "flags" / "dark-mode.toml").exists()

    @respx.mock
    async def test_written_toml_is_valid(self, repo: RepoAccess) -> None:
        import tomllib

        respx.get(f"{_BASE}/api/admin/projects/default/features").mock(
            return_value=_features_response(
                _feature_payload("my-flag", description="A flag", type="experiment")
            )
        )
        adapter = UnleashAdapter()
        await adapter.pull(repo, _CONFIG)

        content = (repo.root / "flags" / "my-flag.toml").read_text()
        data = tomllib.loads(content)
        assert data["name"] == "my-flag"
        assert data["type"] == "experiment"

    @respx.mock
    async def test_skips_unchanged_flags(self, repo: RepoAccess) -> None:
        # Write the exact same content that pull would write.
        from vault_gen.sync.adapters.unleash import FlagSpec, _flag_to_toml

        flag = FlagSpec(name="stable", description="", type="release", enabled=True)
        (repo.root / "flags").mkdir()
        (repo.root / "flags" / "stable.toml").write_text(_flag_to_toml(flag))

        respx.get(f"{_BASE}/api/admin/projects/default/features").mock(
            return_value=_features_response(_feature_payload("stable"))
        )
        adapter = UnleashAdapter()
        result = await adapter.pull(repo, _CONFIG)

        assert result.changes == ()

    @respx.mock
    async def test_updates_changed_flags(self, repo: RepoAccess) -> None:
        (repo.root / "flags").mkdir()
        (repo.root / "flags" / "x.toml").write_text(
            'name = "x"\ndescription = "old"\ntype = "release"\nenabled = true\n'
        )
        respx.get(f"{_BASE}/api/admin/projects/default/features").mock(
            return_value=_features_response(_feature_payload("x", description="new"))
        )
        adapter = UnleashAdapter()
        result = await adapter.pull(repo, _CONFIG)

        assert len(result.changes) == 1
        assert result.changes[0].action == "update"

    @respx.mock
    async def test_pull_auto_commits(self, repo: RepoAccess) -> None:
        respx.get(f"{_BASE}/api/admin/projects/default/features").mock(
            return_value=_features_response(_feature_payload("committed-flag"))
        )
        adapter = UnleashAdapter()
        await adapter.pull(repo, _CONFIG)

        # The file should be committed (git status clean for that file).
        result = subprocess.run(
            ["git", "status", "--short", "flags/committed-flag.toml"],
            cwd=repo.root,
            capture_output=True,
            text=True,
        )
        # Committed files don't appear in short status output.
        assert result.stdout.strip() == ""

    @respx.mock
    async def test_returns_failure_on_http_error(self, repo: RepoAccess) -> None:
        respx.get(f"{_BASE}/api/admin/projects/default/features").mock(
            return_value=httpx.Response(401, json={"message": "Unauthorized"})
        )
        adapter = UnleashAdapter()
        result = await adapter.pull(repo, _CONFIG)
        assert result.success is False
        assert "401" in (result.error or "")


# ---------------------------------------------------------------------------
# Token missing
# ---------------------------------------------------------------------------


class TestTokenRequired:
    @respx.mock
    async def test_push_raises_without_token(
        self, repo: RepoAccess, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VAULT_GEN_UNLEASH_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="VAULT_GEN_UNLEASH_TOKEN"):
            await UnleashAdapter().push(repo, _CONFIG)

    @respx.mock
    async def test_pull_raises_without_token(
        self, repo: RepoAccess, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VAULT_GEN_UNLEASH_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="VAULT_GEN_UNLEASH_TOKEN"):
            await UnleashAdapter().pull(repo, _CONFIG)
