"""Tests for sdk.internal.paths helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from obscura.core.paths import (
    resolve_obscura_home,
    resolve_obscura_mcp_dir,
    resolve_obscura_skills_dir,
)


def test_resolve_obscura_home_prefers_local_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True)
    (project_dir / ".obscura").mkdir()
    monkeypatch.chdir(project_dir)
    monkeypatch.delenv("OBSCURA_HOME", raising=False)

    home = resolve_obscura_home()
    assert home == (project_dir / ".obscura").resolve()


def test_resolve_obscura_home_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_home = tmp_path / "custom-obscura"
    monkeypatch.setenv("OBSCURA_HOME", str(env_home))

    home = resolve_obscura_home()
    assert home == env_home.resolve()


def test_subdirectories_follow_obscura_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_home = tmp_path / "custom-obscura"
    monkeypatch.setenv("OBSCURA_HOME", str(env_home))

    assert resolve_obscura_mcp_dir() == env_home.resolve() / "mcp"
    assert resolve_obscura_skills_dir() == env_home.resolve() / "skills"
