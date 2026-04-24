from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from obscura.vault_gen.generator import RepoConfig, RepoType, generate_repo


def _git_log(path: Path) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%s"], cwd=path, capture_output=True, text=True
    )
    return result.stdout.strip().splitlines()


class TestGenerateConfig:
    def test_creates_directory(self, tmp_path: Path) -> None:
        config = RepoConfig(name="test-config", repo_type=RepoType.CONFIG, destination=tmp_path)
        repo_path = generate_repo(config)
        assert repo_path == tmp_path / "test-config"
        assert repo_path.is_dir()

    def test_git_repo_initialized(self, tmp_path: Path) -> None:
        config = RepoConfig(name="test-config", repo_type=RepoType.CONFIG, destination=tmp_path)
        repo_path = generate_repo(config)
        assert (repo_path / ".git").is_dir()

    def test_initial_commit_exists(self, tmp_path: Path) -> None:
        config = RepoConfig(name="test-config", repo_type=RepoType.CONFIG, destination=tmp_path)
        repo_path = generate_repo(config)
        log = _git_log(repo_path)
        assert len(log) == 1
        assert "test-config" in log[0]

    def test_shared_files_present(self, tmp_path: Path) -> None:
        config = RepoConfig(name="test-config", repo_type=RepoType.CONFIG, destination=tmp_path)
        repo_path = generate_repo(config)
        assert (repo_path / ".gitignore").exists()
        assert (repo_path / "CLAUDE.md").exists()

    def test_claude_md_substitution(self, tmp_path: Path) -> None:
        config = RepoConfig(name="my-fleet", repo_type=RepoType.CONFIG, destination=tmp_path)
        repo_path = generate_repo(config)
        content = (repo_path / "CLAUDE.md").read_text()
        assert "my-fleet" in content
        assert "config" in content
        assert "${name}" not in content

    def test_obsidian_directory(self, tmp_path: Path) -> None:
        config = RepoConfig(name="test-config", repo_type=RepoType.CONFIG, destination=tmp_path)
        repo_path = generate_repo(config)
        assert (repo_path / ".obsidian").is_dir()
        assert (repo_path / ".obsidian" / "app.json").exists()
        assert (repo_path / ".obsidian" / "community-plugins.json").exists()

    def test_config_structure(self, tmp_path: Path) -> None:
        config = RepoConfig(name="test-config", repo_type=RepoType.CONFIG, destination=tmp_path)
        repo_path = generate_repo(config)
        assert (repo_path / "agents").is_dir()
        assert (repo_path / "workspaces").is_dir()
        assert (repo_path / "policies").is_dir()
        assert (repo_path / "tools").is_dir()
        assert (repo_path / "plugins").is_dir()
        assert (repo_path / "env").is_dir()

    def test_access_layer_embedded(self, tmp_path: Path) -> None:
        config = RepoConfig(name="test-config", repo_type=RepoType.CONFIG, destination=tmp_path)
        repo_path = generate_repo(config)
        assert (repo_path / "_access").is_dir()
        assert (repo_path / "_access" / "__init__.py").exists()
        assert (repo_path / "_access" / "repo.py").exists()

    def test_access_layer_is_importable(self, tmp_path: Path) -> None:
        config = RepoConfig(name="test-config", repo_type=RepoType.CONFIG, destination=tmp_path)
        repo_path = generate_repo(config)
        probe = (
            "import sys; sys.path.insert(0, '.'); "
            "from _access import RepoAccess; "
            "r = RepoAccess('.'); print('ok')"
        )
        result = subprocess.run(
            ["python3", "-c", probe],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "ok" in result.stdout

    def test_no_obsidian_dir_in_source_tree(self, tmp_path: Path) -> None:
        """The templates/config/obsidian/ dir must map to .obsidian/, not obsidian/."""
        config = RepoConfig(name="test-config", repo_type=RepoType.CONFIG, destination=tmp_path)
        repo_path = generate_repo(config)
        assert not (repo_path / "obsidian").exists()


class TestGenerateVault:
    def test_creates_directory(self, tmp_path: Path) -> None:
        config = RepoConfig(name="test-vault", repo_type=RepoType.VAULT, destination=tmp_path)
        repo_path = generate_repo(config)
        assert repo_path == tmp_path / "test-vault"

    def test_vault_structure(self, tmp_path: Path) -> None:
        config = RepoConfig(name="test-vault", repo_type=RepoType.VAULT, destination=tmp_path)
        repo_path = generate_repo(config)
        assert (repo_path / "Templates").is_dir()
        assert (repo_path / "Agents").is_dir()
        assert (repo_path / "Memory").is_dir()
        assert (repo_path / "Logs").is_dir()
        assert (repo_path / "Projects").is_dir()

    def test_obsidian_vault_plugins(self, tmp_path: Path) -> None:
        config = RepoConfig(name="test-vault", repo_type=RepoType.VAULT, destination=tmp_path)
        repo_path = generate_repo(config)
        assert (repo_path / ".obsidian" / "daily-notes.json").exists()
        assert (repo_path / ".obsidian" / "templates.json").exists()

    def test_note_templates_present(self, tmp_path: Path) -> None:
        config = RepoConfig(name="test-vault", repo_type=RepoType.VAULT, destination=tmp_path)
        repo_path = generate_repo(config)
        templates = list((repo_path / "Templates").glob("*.md"))
        assert len(templates) == 4

    def test_git_repo_initialized(self, tmp_path: Path) -> None:
        config = RepoConfig(name="test-vault", repo_type=RepoType.VAULT, destination=tmp_path)
        repo_path = generate_repo(config)
        assert (repo_path / ".git").is_dir()


class TestMeta:
    def test_meta_file_written_for_config(self, tmp_path: Path) -> None:
        config = RepoConfig(name="meta-config", repo_type=RepoType.CONFIG, destination=tmp_path)
        repo_path = generate_repo(config)
        meta = repo_path / ".vault-gen" / "meta.toml"
        assert meta.exists()
        content = meta.read_text()
        assert 'type = "config"' in content
        assert 'name = "meta-config"' in content

    def test_meta_file_written_for_vault(self, tmp_path: Path) -> None:
        config = RepoConfig(name="meta-vault", repo_type=RepoType.VAULT, destination=tmp_path)
        repo_path = generate_repo(config)
        meta = repo_path / ".vault-gen" / "meta.toml"
        assert meta.exists()
        assert 'type = "vault"' in meta.read_text()

    def test_meta_enables_permission_model(self, tmp_path: Path) -> None:
        """RepoAccess should pick up the meta and gate writes on config repos."""
        from obscura.vault_gen.access.repo import RepoAccess

        config = RepoConfig(name="perm-config", repo_type=RepoType.CONFIG, destination=tmp_path)
        repo_path = generate_repo(config)

        access = RepoAccess(repo_path)
        with pytest.raises(PermissionError):
            access.write("agents/test.md", "content")

        admin_access = RepoAccess(repo_path, privilege="admin")
        admin_access.write("agents/test.md", "content", commit_msg="test")
        assert (repo_path / "agents" / "test.md").exists()


class TestObsidianPlugins:
    def test_config_has_obsidian_git_plugin_data(self, tmp_path: Path) -> None:
        config = RepoConfig(name="plug-config", repo_type=RepoType.CONFIG, destination=tmp_path)
        repo_path = generate_repo(config)
        plugin_data = repo_path / ".obsidian" / "plugins" / "obsidian-git" / "data.json"
        assert plugin_data.exists()

    def test_vault_has_obsidian_git_plugin_data(self, tmp_path: Path) -> None:
        config = RepoConfig(name="plug-vault", repo_type=RepoType.VAULT, destination=tmp_path)
        repo_path = generate_repo(config)
        plugin_data = repo_path / ".obsidian" / "plugins" / "obsidian-git" / "data.json"
        assert plugin_data.exists()

    def test_vault_git_plugin_auto_commit_enabled(self, tmp_path: Path) -> None:
        import json

        config = RepoConfig(name="auto-vault", repo_type=RepoType.VAULT, destination=tmp_path)
        repo_path = generate_repo(config)
        data = json.loads(
            (repo_path / ".obsidian" / "plugins" / "obsidian-git" / "data.json").read_text()
        )
        assert data["autoSaveInterval"] >= 1
        assert data["autoPullOnBoot"] is True
        assert data["disablePush"] is False

    def test_vault_has_daily_notes_config(self, tmp_path: Path) -> None:
        import json

        config = RepoConfig(name="dn-vault", repo_type=RepoType.VAULT, destination=tmp_path)
        repo_path = generate_repo(config)
        dn = json.loads((repo_path / ".obsidian" / "daily-notes.json").read_text())
        assert dn["folder"] == "Logs"
        assert "Daily Note" in dn["template"]

    def test_vault_has_templates_config(self, tmp_path: Path) -> None:
        import json

        config = RepoConfig(name="tpl-vault", repo_type=RepoType.VAULT, destination=tmp_path)
        repo_path = generate_repo(config)
        tpl = json.loads((repo_path / ".obsidian" / "templates.json").read_text())
        assert tpl["folder"] == "Templates"


class TestGenerateErrors:
    def test_raises_if_dest_exists(self, tmp_path: Path) -> None:
        config = RepoConfig(name="existing", repo_type=RepoType.CONFIG, destination=tmp_path)
        generate_repo(config)
        with pytest.raises(FileExistsError, match="already exists"):
            generate_repo(config)

    def test_cleans_up_on_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Partial directory should be removed if generation fails mid-way."""
        import obscura.vault_gen.generator as gen_mod

        def failing_git_init(*args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated git failure")

        monkeypatch.setattr(gen_mod, "_git_init", failing_git_init)

        config = RepoConfig(name="fail-test", repo_type=RepoType.CONFIG, destination=tmp_path)
        with pytest.raises(RuntimeError, match="simulated git failure"):
            generate_repo(config)

        assert not (tmp_path / "fail-test").exists()
