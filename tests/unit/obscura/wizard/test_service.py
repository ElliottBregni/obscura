"""Tests for obscura.wizard.service.WizardService.

The service is the single source of truth that the TUI, FastAPI router,
and MCP tools all consume — so the contract verified here pins all three.
"""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

import pytest

from obscura.wizard import Profile, WizardService


@pytest.fixture
def svc(tmp_path: Path) -> WizardService:
    return WizardService(config_dir=tmp_path)


# ----------------------------------------------------------------------
# Empty-state behaviour
# ----------------------------------------------------------------------


class TestEmptyState:
    def test_snapshot_on_missing_config(self, svc: WizardService) -> None:
        snap = svc.snapshot()
        assert snap.profiles == []
        assert snap.active.profile == "default"
        assert snap.workspaces == []
        # Discovery still works even with no config dir.
        assert "copilot" in snap.available_backends
        assert "shell.exec" in snap.available_capabilities

    def test_get_profile_missing_returns_none(self, svc: WizardService) -> None:
        assert svc.get_profile("does-not-exist") is None

    def test_delete_missing_profile_returns_false(self, svc: WizardService) -> None:
        assert svc.delete_profile("nope") is False

    def test_unset_missing_workspace_returns_false(self, svc: WizardService) -> None:
        assert svc.unset_workspace("/no/such/path") is False


# ----------------------------------------------------------------------
# Profile CRUD round-trip
# ----------------------------------------------------------------------


class TestProfileCRUD:
    def test_upsert_then_get(self, svc: WizardService) -> None:
        p = Profile(
            name="research",
            prompts=["soul_default", "runtime"],
            backend="claude",
            capabilities=["file.read", "web.browse"],
        )
        svc.upsert_profile(p)

        loaded = svc.get_profile("research")
        assert loaded is not None
        assert loaded.name == "research"
        assert loaded.backend == "claude"
        assert loaded.prompts == ["soul_default", "runtime"]
        assert loaded.capabilities == ["file.read", "web.browse"]

    def test_upsert_overwrites(self, svc: WizardService) -> None:
        svc.upsert_profile(Profile(name="x", backend="copilot"))
        svc.upsert_profile(Profile(name="x", backend="claude"))
        loaded = svc.get_profile("x")
        assert loaded is not None and loaded.backend == "claude"

    def test_list_profiles_sorted(self, svc: WizardService) -> None:
        svc.upsert_profile(Profile(name="zebra"))
        svc.upsert_profile(Profile(name="alpha"))
        svc.upsert_profile(Profile(name="mango"))
        assert [p.name for p in svc.list_profiles()] == ["alpha", "mango", "zebra"]

    def test_delete_returns_true_and_removes(self, svc: WizardService) -> None:
        svc.upsert_profile(Profile(name="tmp"))
        assert svc.delete_profile("tmp") is True
        assert svc.get_profile("tmp") is None

    def test_delete_active_profile_falls_back_to_default(
        self, svc: WizardService
    ) -> None:
        svc.upsert_profile(Profile(name="tmp"))
        svc.set_active("tmp")
        svc.delete_profile("tmp")
        assert svc.get_active().profile == "default"


# ----------------------------------------------------------------------
# Active profile + workspace bindings
# ----------------------------------------------------------------------


class TestActiveAndWorkspaces:
    def test_set_active_round_trip(self, svc: WizardService) -> None:
        svc.set_active("research")
        assert svc.get_active().profile == "research"

    def test_set_workspace_round_trip(self, svc: WizardService) -> None:
        svc.set_workspace("/a/b", "research")
        bindings = svc.list_workspaces()
        assert len(bindings) == 1
        assert bindings[0].path == "/a/b"
        assert bindings[0].profile == "research"

    def test_unset_workspace(self, svc: WizardService) -> None:
        svc.set_workspace("/a/b", "research")
        assert svc.unset_workspace("/a/b") is True
        assert svc.list_workspaces() == []


# ----------------------------------------------------------------------
# Active-profile resolution precedence
# ----------------------------------------------------------------------


class TestResolveActiveProfile:
    def test_returns_none_when_no_profile_defined(
        self,
        svc: WizardService,
        tmp_path: Path,
    ) -> None:
        # No [profiles.*] sections at all -> resolution returns None.
        assert svc.resolve_active_profile(cwd=tmp_path) is None

    def test_active_field_resolves_when_profile_exists(
        self,
        svc: WizardService,
        tmp_path: Path,
    ) -> None:
        svc.upsert_profile(Profile(name="default", backend="copilot"))
        # No active set, no workspace binding -> falls through to "default".
        resolved = svc.resolve_active_profile(cwd=tmp_path)
        assert resolved is not None and resolved.name == "default"

    def test_set_active_takes_precedence_over_default(
        self,
        svc: WizardService,
        tmp_path: Path,
    ) -> None:
        svc.upsert_profile(Profile(name="default"))
        svc.upsert_profile(Profile(name="research", backend="claude"))
        svc.set_active("research")
        resolved = svc.resolve_active_profile(cwd=tmp_path)
        assert resolved is not None and resolved.name == "research"

    def test_env_var_overrides_active(
        self,
        svc: WizardService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        svc.upsert_profile(Profile(name="default"))
        svc.upsert_profile(Profile(name="research"))
        svc.upsert_profile(Profile(name="coding", backend="codex"))
        svc.set_active("research")
        monkeypatch.setenv("OBSCURA_PROFILE", "coding")
        resolved = svc.resolve_active_profile(cwd=tmp_path)
        assert resolved is not None and resolved.name == "coding"

    def test_workspace_binding_overrides_active(
        self,
        svc: WizardService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OBSCURA_PROFILE", raising=False)
        svc.upsert_profile(Profile(name="default"))
        svc.upsert_profile(Profile(name="research"))
        svc.set_active("default")
        svc.set_workspace(str(tmp_path), "research")
        resolved = svc.resolve_active_profile(cwd=tmp_path)
        assert resolved is not None and resolved.name == "research"

    def test_longest_workspace_match_wins(
        self,
        svc: WizardService,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OBSCURA_PROFILE", raising=False)
        svc.upsert_profile(Profile(name="general"))
        svc.upsert_profile(Profile(name="specific"))
        # Both ancestor of cwd; the more-specific one should win.
        nested = tmp_path / "sub" / "leaf"
        nested.mkdir(parents=True)
        svc.set_workspace(str(tmp_path), "general")
        svc.set_workspace(str(tmp_path / "sub"), "specific")
        resolved = svc.resolve_active_profile(cwd=nested)
        assert resolved is not None and resolved.name == "specific"


# ----------------------------------------------------------------------
# Atomic-write preservation
# ----------------------------------------------------------------------


class TestAtomicWritePreservesOtherSections:
    def test_existing_sections_untouched(
        self,
        svc: WizardService,
        tmp_path: Path,
    ) -> None:
        # Seed a config.toml with non-wizard sections — same shape the user
        # has in production.
        (tmp_path / "config.toml").write_text(
            """
mode = "code"

[plugins]
load_builtins = true
include = ["ripgrep", "fd"]

[plugins.bootstrap]
auto_install = true

[defaults.capabilities]
grant = ["shell.exec", "file.read"]
deny = []

[mcp]
auto_discover = true
""",
            encoding="utf-8",
        )

        svc.upsert_profile(Profile(name="research", backend="claude"))
        svc.set_active("research")

        with (tmp_path / "config.toml").open("rb") as f:
            after = tomllib.load(f)

        assert after["mode"] == "code"
        assert after["plugins"]["load_builtins"] is True
        assert after["plugins"]["include"] == ["ripgrep", "fd"]
        assert after["plugins"]["bootstrap"]["auto_install"] is True
        assert after["defaults"]["capabilities"]["grant"] == ["shell.exec", "file.read"]
        assert after["mcp"]["auto_discover"] is True
        assert after["profiles"]["research"]["backend"] == "claude"
        assert after["active"]["profile"] == "research"


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------


class TestDiscovery:
    def test_user_overlay_prompts_are_discovered(
        self,
        svc: WizardService,
        tmp_path: Path,
    ) -> None:
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "custom.md").write_text("# Custom\nhello", encoding="utf-8")
        (prompts_dir / "other.txt").write_text("other", encoding="utf-8")
        names = svc.list_available_prompts()
        assert "custom" in names
        assert "other" in names

    def test_user_granted_capabilities_surface_even_if_unknown(
        self,
        svc: WizardService,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "config.toml").write_text(
            """
[defaults.capabilities]
grant = ["custom.thing", "shell.exec"]
""",
            encoding="utf-8",
        )
        caps = svc.list_available_capabilities()
        assert "custom.thing" in caps
        assert "shell.exec" in caps

    def test_load_profile_prompt_text_loads_user_overlay(
        self,
        svc: WizardService,
        tmp_path: Path,
    ) -> None:
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "intro.md").write_text("PROMPT-CONTENT", encoding="utf-8")
        profile = Profile(name="x", prompts=["intro", "missing-on-purpose"])
        loaded = svc.load_profile_prompt_text(profile)
        assert loaded == ["PROMPT-CONTENT"]


# ----------------------------------------------------------------------
# Per-profile env file
# ----------------------------------------------------------------------


class TestEnvFile:
    def test_read_missing_returns_empty(self, svc: WizardService) -> None:
        assert svc.read_env_file("research") == ""

    def test_round_trip(self, svc: WizardService, tmp_path: Path) -> None:
        path = svc.write_env_file("research", "FOO=bar\nBAZ=qux\n")
        assert path == tmp_path / ".env.research"
        assert svc.read_env_file("research") == "FOO=bar\nBAZ=qux\n"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
    def test_written_with_owner_only_perms(self, svc: WizardService) -> None:
        path = svc.write_env_file("research", "SECRET=hunter2\n")
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600


# ----------------------------------------------------------------------
# External-editor flow (TUI helper)
# ----------------------------------------------------------------------


class TestExternalEditor:
    """Use a tiny shell script as $EDITOR so we don't depend on vi being on $PATH."""

    def _make_editor(
        self,
        tmp_path: Path,
        action: str,
    ) -> str:
        script = tmp_path / "fake_editor.sh"
        script.write_text(f"#!/bin/sh\n{action}\n", encoding="utf-8")
        script.chmod(0o755)
        return str(script)

    def test_returns_seed_when_editor_makes_no_changes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.wizard.tui import _edit_in_external_editor

        monkeypatch.setenv("EDITOR", self._make_editor(tmp_path, "true"))
        out = _edit_in_external_editor("seed-content\n", suffix=".env")
        assert out == "seed-content\n"

    def test_returns_modified_content_when_editor_writes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.wizard.tui import _edit_in_external_editor

        # The fake editor receives the tempfile path as $1 and rewrites it.
        editor = self._make_editor(tmp_path, 'printf "FOO=bar\\nBAZ=qux\\n" > "$1"')
        monkeypatch.setenv("EDITOR", editor)
        out = _edit_in_external_editor("seed-content\n", suffix=".env")
        assert out == "FOO=bar\nBAZ=qux\n"

    def test_returns_none_when_editor_unset_and_vi_unavailable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.wizard.tui import _edit_in_external_editor

        monkeypatch.delenv("EDITOR", raising=False)
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.setenv("PATH", "")  # so "vi" cannot be found
        out = _edit_in_external_editor("seed", suffix=".env")
        assert out is None

    def test_supports_editor_with_arguments(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from obscura.wizard.tui import _edit_in_external_editor

        # Simulate `EDITOR="code --wait"` style by chaining args via env.
        editor = self._make_editor(
            tmp_path,
            'test "$1" = "--wait" && printf "edited\\n" > "$2"',
        )
        monkeypatch.setenv("EDITOR", f"{editor} --wait")
        out = _edit_in_external_editor("seed", suffix=".env")
        assert out == "edited\n"


# ----------------------------------------------------------------------
# Extended fields: mode / skills / vault_path
# ----------------------------------------------------------------------


class TestExtendedProfileFields:
    def test_mode_round_trips(self, svc: WizardService) -> None:
        svc.upsert_profile(Profile(name="research", mode="plan"))
        loaded = svc.get_profile("research")
        assert loaded is not None and loaded.mode == "plan"

    def test_skills_round_trip(self, svc: WizardService) -> None:
        svc.upsert_profile(
            Profile(name="research", skills=["api-design", "code-review"]),
        )
        loaded = svc.get_profile("research")
        assert loaded is not None
        assert loaded.skills == ["api-design", "code-review"]

    def test_vault_path_round_trip(self, svc: WizardService) -> None:
        svc.upsert_profile(Profile(name="research", vault_path="/tmp/vault-x"))
        loaded = svc.get_profile("research")
        assert loaded is not None and loaded.vault_path == "/tmp/vault-x"

    def test_omitted_fields_stay_none(self, svc: WizardService) -> None:
        svc.upsert_profile(Profile(name="x"))
        loaded = svc.get_profile("x")
        assert loaded is not None
        assert loaded.mode is None
        assert loaded.vault_path is None
        assert loaded.skills == []


# ----------------------------------------------------------------------
# Extended discovery: skills, modes, commands, hooks_summary
# ----------------------------------------------------------------------


class TestExtendedDiscovery:
    def test_skills_discovered(self, svc: WizardService, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        (skills_dir / "nested").mkdir(parents=True)
        (skills_dir / "alpha.md").write_text("# alpha", encoding="utf-8")
        (skills_dir / "nested" / "beta.md").write_text("# beta", encoding="utf-8")
        names = svc.list_available_skills()
        assert "alpha" in names
        assert "beta" in names

    def test_commands_discovered(self, svc: WizardService, tmp_path: Path) -> None:
        cmd_dir = tmp_path / "commands"
        cmd_dir.mkdir()
        (cmd_dir / "foo.md").write_text("body", encoding="utf-8")
        assert "foo" in svc.list_available_commands()

    def test_modes_static(self, svc: WizardService) -> None:
        snap = svc.snapshot()
        assert snap.available_modes == ["code", "ask", "plan", "diff"]

    def test_hooks_summary_counts_handlers(
        self,
        svc: WizardService,
        tmp_path: Path,
    ) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "hooks.json").write_text(
            '{"on_session_start": [{"x": 1}, {"x": 2}], "on_tool_call": [{"x": 1}]}',
            encoding="utf-8",
        )
        summary = svc.hooks_summary()
        assert summary == {"on_session_start": 2, "on_tool_call": 1}

    def test_hooks_summary_returns_empty_when_missing(
        self,
        svc: WizardService,
    ) -> None:
        assert svc.hooks_summary() == {}


# ----------------------------------------------------------------------
# SOUL.md round-trip
# ----------------------------------------------------------------------


class TestSoulFile:
    def test_read_missing_returns_empty(self, svc: WizardService) -> None:
        assert svc.read_soul() == ""

    def test_round_trip(self, svc: WizardService, tmp_path: Path) -> None:
        path = svc.write_soul("# SOUL\nhello\n")
        assert path == tmp_path / "SOUL.md"
        assert svc.read_soul() == "# SOUL\nhello\n"

    def test_default_vault_path_is_under_config_dir(
        self,
        svc: WizardService,
        tmp_path: Path,
    ) -> None:
        assert svc.default_vault_path() == tmp_path / "vault"


# ----------------------------------------------------------------------
# Profile -> environment application
# ----------------------------------------------------------------------


class TestApplyProfileToEnvironment:
    def test_mode_and_vault_set(
        self,
        svc: WizardService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OBSCURA_MODE", raising=False)
        monkeypatch.delenv("OBSCURA_VAULT_DIR", raising=False)
        svc.apply_profile_to_environment(
            Profile(name="x", mode="plan", vault_path="/tmp/v"),
        )
        assert os.environ["OBSCURA_MODE"] == "plan"
        assert os.environ["OBSCURA_VAULT_DIR"] == "/tmp/v"

    def test_does_not_overwrite_existing_env(
        self,
        svc: WizardService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OBSCURA_MODE", "code")
        monkeypatch.setenv("OBSCURA_VAULT_DIR", "/already/set")
        svc.apply_profile_to_environment(
            Profile(name="x", mode="plan", vault_path="/tmp/v"),
        )
        # Existing values should win.
        assert os.environ["OBSCURA_MODE"] == "code"
        assert os.environ["OBSCURA_VAULT_DIR"] == "/already/set"
