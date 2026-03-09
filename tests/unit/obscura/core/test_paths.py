"""Tests for obscura.core.paths — Path resolution helpers."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from obscura.core.paths import (
    resolve_agents_sessions_dir,
    resolve_obscura_home,
    resolve_obscura_hooks_dir,
    resolve_obscura_mcp_dir,
    resolve_obscura_settings,
    resolve_obscura_skills_dir,
    resolve_obscura_specs_dir,
    resolve_obscura_state_dir,
)


class TestResolveObscuraHome:
    def test_env_var_takes_priority(self, tmp_path: Path) -> None:
        with mock.patch.dict(os.environ, {"OBSCURA_HOME": str(tmp_path)}):
            assert resolve_obscura_home() == tmp_path

    def test_local_obscura_dir(self, tmp_path: Path) -> None:
        local = tmp_path / ".obscura"
        local.mkdir()
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("OBSCURA_HOME", None)
            result = resolve_obscura_home(tmp_path)
            assert result == local

    def test_falls_back_to_global(self, tmp_path: Path) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("OBSCURA_HOME", None)
            result = resolve_obscura_home(tmp_path)
            assert result == (Path.home() / ".obscura").resolve()


class TestResolveSpecsDir:
    def test_returns_specs_subdir(self, tmp_path: Path) -> None:
        with mock.patch.dict(os.environ, {"OBSCURA_HOME": str(tmp_path)}):
            assert resolve_obscura_specs_dir() == tmp_path / "specs"

    def test_with_cwd(self, tmp_path: Path) -> None:
        local = tmp_path / ".obscura"
        local.mkdir()
        result = resolve_obscura_specs_dir(tmp_path)
        assert result == local / "specs"


class TestResolveStateDir:
    def test_returns_state_subdir(self, tmp_path: Path) -> None:
        with mock.patch.dict(os.environ, {"OBSCURA_HOME": str(tmp_path)}):
            assert resolve_obscura_state_dir() == tmp_path / "state"


class TestOtherResolvers:
    def test_mcp_dir(self, tmp_path: Path) -> None:
        with mock.patch.dict(os.environ, {"OBSCURA_HOME": str(tmp_path)}):
            assert resolve_obscura_mcp_dir() == tmp_path / "mcp"

    def test_skills_dir(self, tmp_path: Path) -> None:
        with mock.patch.dict(os.environ, {"OBSCURA_HOME": str(tmp_path)}):
            assert resolve_obscura_skills_dir() == tmp_path / "skills"

    def test_hooks_dir(self, tmp_path: Path) -> None:
        with mock.patch.dict(os.environ, {"OBSCURA_HOME": str(tmp_path)}):
            assert resolve_obscura_hooks_dir() == tmp_path / "hooks"

    def test_sessions_dir(self, tmp_path: Path) -> None:
        with mock.patch.dict(os.environ, {"OBSCURA_HOME": str(tmp_path)}):
            assert resolve_agents_sessions_dir() == tmp_path / "agents" / "sessions"

    def test_settings(self, tmp_path: Path) -> None:
        with mock.patch.dict(os.environ, {"OBSCURA_HOME": str(tmp_path)}):
            assert resolve_obscura_settings() == tmp_path / "settings.json"
