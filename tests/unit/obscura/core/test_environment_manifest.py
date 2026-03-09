"""Tests for EnvironmentManifest and CompiledAgent.env integration."""

from __future__ import annotations

import dataclasses

import pytest

from obscura.core.compiler.compiled import (
    CompiledAgent,
    EnvironmentManifest,
)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestEnvironmentManifestDefaults:
    def test_default_python_version(self) -> None:
        m = EnvironmentManifest()
        assert m.python_version == "3.13"

    def test_default_packages_empty(self) -> None:
        m = EnvironmentManifest()
        assert m.packages == ()

    def test_default_env_vars_empty(self) -> None:
        m = EnvironmentManifest()
        assert m.env_vars == ()

    def test_default_binaries_empty(self) -> None:
        m = EnvironmentManifest()
        assert m.binaries == ()

    def test_default_working_dir_empty(self) -> None:
        m = EnvironmentManifest()
        assert m.working_dir == ""

    def test_default_network_mode(self) -> None:
        m = EnvironmentManifest()
        assert m.network_mode == "unrestricted"

    def test_default_network_allow_empty(self) -> None:
        m = EnvironmentManifest()
        assert m.network_allow == ()

    def test_default_read_paths_empty(self) -> None:
        m = EnvironmentManifest()
        assert m.read_paths == ()

    def test_default_write_paths_empty(self) -> None:
        m = EnvironmentManifest()
        assert m.write_paths == ()

    def test_default_timeout_seconds(self) -> None:
        m = EnvironmentManifest()
        assert m.timeout_seconds == 600.0

    def test_default_max_iterations(self) -> None:
        m = EnvironmentManifest()
        assert m.max_iterations == 25


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestEnvironmentManifestImmutability:
    def test_frozen(self) -> None:
        m = EnvironmentManifest()
        with pytest.raises(dataclasses.FrozenInstanceError):
            m.python_version = "3.12"  # type: ignore[misc]

    def test_frozen_packages(self) -> None:
        m = EnvironmentManifest()
        with pytest.raises(dataclasses.FrozenInstanceError):
            m.packages = ("requests",)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Custom values
# ---------------------------------------------------------------------------


class TestEnvironmentManifestCustom:
    def test_all_fields_populated(self) -> None:
        m = EnvironmentManifest(
            python_version="3.12",
            packages=("requests>=2.31.0", "httpx>=0.27.0"),
            env_vars=(("API_KEY", "secret"), ("DEBUG", "1")),
            binaries=("gws-cli", "ruff"),
            working_dir="/workspace",
            network_mode="restricted",
            network_allow=("api.github.com", "pypi.org"),
            read_paths=("./src",),
            write_paths=("./output",),
            timeout_seconds=300.0,
            max_iterations=50,
        )
        assert m.python_version == "3.12"
        assert len(m.packages) == 2
        assert len(m.env_vars) == 2
        assert len(m.binaries) == 2
        assert m.working_dir == "/workspace"
        assert m.network_mode == "restricted"
        assert len(m.network_allow) == 2
        assert m.timeout_seconds == 300.0
        assert m.max_iterations == 50

    def test_packages_as_tuple(self) -> None:
        m = EnvironmentManifest(packages=("pydantic==2.0",))
        assert isinstance(m.packages, tuple)
        assert m.packages[0] == "pydantic==2.0"

    def test_env_vars_as_tuple_of_pairs(self) -> None:
        m = EnvironmentManifest(env_vars=(("KEY", "val"),))
        assert isinstance(m.env_vars, tuple)
        key, val = m.env_vars[0]
        assert key == "KEY"
        assert val == "val"


# ---------------------------------------------------------------------------
# CompiledAgent integration
# ---------------------------------------------------------------------------


def _minimal_agent(**overrides) -> CompiledAgent:
    defaults = dict(
        name="test-agent",
        template_name="base",
        mode="code",
        agent_type="loop",
        provider="claude",
    )
    defaults.update(overrides)
    return CompiledAgent(**defaults)


class TestCompiledAgentWithEnv:
    def test_agent_env_default_none(self) -> None:
        agent = _minimal_agent()
        assert agent.env is None

    def test_agent_with_env_manifest(self) -> None:
        env = EnvironmentManifest(
            python_version="3.13",
            binaries=("pyright",),
        )
        agent = _minimal_agent(env=env)
        assert agent.env is not None
        assert agent.env.python_version == "3.13"
        assert agent.env.binaries == ("pyright",)

    def test_agent_env_frozen(self) -> None:
        env = EnvironmentManifest()
        agent = _minimal_agent(env=env)
        with pytest.raises(dataclasses.FrozenInstanceError):
            agent.env = None  # type: ignore[misc]
