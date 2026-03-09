"""Tests for obscura.core.compiler.loader — YAML spec loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from obscura.core.compiler.errors import SpecLoadError
from obscura.core.compiler.loader import SpecRegistry, load_spec_file, load_specs_dir
from obscura.core.compiler.specs import (
    PolicySpec,
    TemplateSpec,
    WorkspaceSpec,
)


@pytest.fixture()
def specs_dir(tmp_path: Path) -> Path:
    """Create a temp directory with spec files."""
    d = tmp_path / "specs"
    d.mkdir()
    return d


def _write_yaml(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


class TestLoadSpecFile:
    def test_load_template(self, specs_dir: Path) -> None:
        path = _write_yaml(
            specs_dir / "code-agent.yml",
            """\
apiVersion: obscura/v1
kind: Template
metadata:
  name: code-agent
  tags: [dev]
spec:
  provider: claude
  plugins:
    - git
    - shell
""",
        )
        spec = load_spec_file(path)
        assert isinstance(spec, TemplateSpec)
        assert spec.metadata.name == "code-agent"
        assert spec.spec.provider == "claude"
        assert spec.spec.plugins == ["git", "shell"]

    def test_load_policy(self, specs_dir: Path) -> None:
        path = _write_yaml(
            specs_dir / "safe.yml",
            """\
apiVersion: obscura/v1
kind: Policy
metadata:
  name: safe-dev
spec:
  tool_denylist:
    - dangerous_tool
  max_turns: 15
""",
        )
        spec = load_spec_file(path)
        assert isinstance(spec, PolicySpec)
        assert spec.spec.max_turns == 15

    def test_load_workspace(self, specs_dir: Path) -> None:
        path = _write_yaml(
            specs_dir / "code-mode.yml",
            """\
apiVersion: obscura/v1
kind: Workspace
metadata:
  name: code-mode
spec:
  policies:
    - safe-dev
  agents:
    - name: dev
      template: code-agent
      mode: task
""",
        )
        spec = load_spec_file(path)
        assert isinstance(spec, WorkspaceSpec)
        assert len(spec.spec.agents) == 1

    def test_missing_file(self, specs_dir: Path) -> None:
        with pytest.raises(SpecLoadError, match="not found"):
            load_spec_file(specs_dir / "nonexistent.yml")

    def test_invalid_yaml(self, specs_dir: Path) -> None:
        path = _write_yaml(specs_dir / "bad.yml", "{{invalid yaml")
        with pytest.raises(SpecLoadError, match="Invalid YAML"):
            load_spec_file(path)

    def test_missing_kind(self, specs_dir: Path) -> None:
        path = _write_yaml(
            specs_dir / "no-kind.yml",
            "apiVersion: obscura/v1\nmetadata:\n  name: test\nspec: {}\n",
        )
        with pytest.raises(SpecLoadError, match="Missing 'kind'"):
            load_spec_file(path)

    def test_unknown_kind(self, specs_dir: Path) -> None:
        path = _write_yaml(
            specs_dir / "unknown.yml",
            "apiVersion: obscura/v1\nkind: Unknown\nmetadata:\n  name: test\nspec: {}\n",
        )
        with pytest.raises(SpecLoadError, match="Unknown kind"):
            load_spec_file(path)

    def test_validation_error(self, specs_dir: Path) -> None:
        path = _write_yaml(
            specs_dir / "invalid.yml",
            """\
apiVersion: obscura/v1
kind: Template
metadata:
  name: test
spec:
  max_iterations: not_a_number
""",
        )
        with pytest.raises(SpecLoadError, match="Validation error"):
            load_spec_file(path)

    def test_not_a_mapping(self, specs_dir: Path) -> None:
        path = _write_yaml(specs_dir / "list.yml", "- item1\n- item2\n")
        with pytest.raises(SpecLoadError, match="Expected a YAML mapping"):
            load_spec_file(path)


class TestLoadSpecsDir:
    def test_loads_all_specs(self, specs_dir: Path) -> None:
        _write_yaml(
            specs_dir / "tmpl.yml",
            "apiVersion: obscura/v1\nkind: Template\nmetadata:\n  name: t1\nspec: {}\n",
        )
        _write_yaml(
            specs_dir / "policy.yaml",
            "apiVersion: obscura/v1\nkind: Policy\nmetadata:\n  name: p1\nspec: {}\n",
        )
        _write_yaml(
            specs_dir / "ws.yml",
            "apiVersion: obscura/v1\nkind: Workspace\nmetadata:\n  name: w1\nspec: {}\n",
        )
        registry = load_specs_dir(specs_dir)
        assert "t1" in registry.templates
        assert "p1" in registry.policies
        assert "w1" in registry.workspaces

    def test_skips_bad_files(self, specs_dir: Path) -> None:
        _write_yaml(
            specs_dir / "good.yml",
            "apiVersion: obscura/v1\nkind: Template\nmetadata:\n  name: good\nspec: {}\n",
        )
        _write_yaml(specs_dir / "bad.yml", "{{invalid")
        registry = load_specs_dir(specs_dir)
        assert "good" in registry.templates
        assert len(registry.templates) == 1

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        registry = load_specs_dir(tmp_path / "nope")
        assert len(registry.templates) == 0

    def test_nested_dirs(self, specs_dir: Path) -> None:
        sub = specs_dir / "templates"
        sub.mkdir()
        _write_yaml(
            sub / "nested.yml",
            "apiVersion: obscura/v1\nkind: Template\nmetadata:\n  name: nested\nspec: {}\n",
        )
        registry = load_specs_dir(specs_dir)
        assert "nested" in registry.templates


class TestSpecRegistry:
    def test_add_and_get(self) -> None:
        registry = SpecRegistry()
        tmpl = TemplateSpec.model_validate({
            "apiVersion": "obscura/v1",
            "kind": "Template",
            "metadata": {"name": "t1"},
            "spec": {},
        })
        registry.add(tmpl)
        assert registry.get_template("t1") is tmpl
        assert registry.get_template("missing") is None
