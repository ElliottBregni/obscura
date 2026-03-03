"""Tests for agent templating and versioning."""

from __future__ import annotations

from pathlib import Path

import pytest

from obscura.core.supervisor.agent_templates import AgentTemplateStore


@pytest.fixture
def store(tmp_path: Path) -> AgentTemplateStore:
    s = AgentTemplateStore(tmp_path / "test.db")
    yield s
    s.close()


class TestAgentTemplateStore:
    def test_create_template(self, store: AgentTemplateStore) -> None:
        tmpl = store.create_template(
            name="code-architect",
            description="Code architecture agent",
            template_json={
                "system_prompt": "You are a {{role}} for {{project}}.",
                "tool_bundles": ["code_tools"],
            },
        )
        assert tmpl.name == "code-architect"
        assert tmpl.template_id
        assert "{{role}}" in tmpl.system_prompt_template

    def test_get_template(self, store: AgentTemplateStore) -> None:
        created = store.create_template(name="test", template_json={"system_prompt": "hi"})
        loaded = store.get_template(created.template_id)
        assert loaded is not None
        assert loaded.name == "test"

    def test_get_template_by_name(self, store: AgentTemplateStore) -> None:
        store.create_template(name="unique-name", template_json={"system_prompt": "hi"})
        loaded = store.get_template_by_name("unique-name")
        assert loaded is not None
        assert loaded.name == "unique-name"

    def test_list_templates(self, store: AgentTemplateStore) -> None:
        store.create_template(name="a", template_json={})
        store.create_template(name="b", template_json={})
        templates = store.list_templates()
        assert len(templates) >= 2

    def test_template_variables_extracted(self, store: AgentTemplateStore) -> None:
        tmpl = store.create_template(
            name="test",
            template_json={
                "system_prompt": "{{role}} for {{project}}",
                "safety": "{{safety_profile}}",
            },
        )
        vars_ = tmpl.variables
        assert "role" in vars_
        assert "project" in vars_
        assert "safety_profile" in vars_


class TestAgentVersions:
    def test_render_version(self, store: AgentTemplateStore) -> None:
        tmpl = store.create_template(
            name="test",
            template_json={
                "system_prompt": "You are a {{role}} for {{project}}.",
                "mode": "{{mode}}",
            },
        )
        ver = store.render_version(
            tmpl.template_id,
            variables={"role": "architect", "project": "Obscura", "mode": "strict"},
        )
        assert ver.version == 1
        assert ver.template_id == tmpl.template_id
        assert "architect" in ver.system_prompt
        assert "Obscura" in ver.system_prompt
        assert ver.hash  # non-empty

    def test_versions_are_immutable(self, store: AgentTemplateStore) -> None:
        tmpl = store.create_template(name="test", template_json={"system_prompt": "{{x}}"})
        v1 = store.render_version(tmpl.template_id, variables={"x": "first"})
        v2 = store.render_version(tmpl.template_id, variables={"x": "second"})

        assert v1.version == 1
        assert v2.version == 2
        assert v1.hash != v2.hash
        assert v1.agent_id != v2.agent_id

    def test_get_version(self, store: AgentTemplateStore) -> None:
        tmpl = store.create_template(name="test", template_json={"system_prompt": "hi"})
        ver = store.render_version(tmpl.template_id)
        loaded = store.get_version(ver.agent_id)
        assert loaded is not None
        assert loaded.hash == ver.hash

    def test_get_latest_version(self, store: AgentTemplateStore) -> None:
        tmpl = store.create_template(name="test", template_json={"system_prompt": "{{x}}"})
        store.render_version(tmpl.template_id, variables={"x": "v1"})
        store.render_version(tmpl.template_id, variables={"x": "v2"})
        latest = store.get_latest_version(tmpl.template_id)
        assert latest is not None
        assert latest.version == 2

    def test_list_versions(self, store: AgentTemplateStore) -> None:
        tmpl = store.create_template(name="test", template_json={"system_prompt": "{{x}}"})
        store.render_version(tmpl.template_id, variables={"x": "v1"})
        store.render_version(tmpl.template_id, variables={"x": "v2"})
        store.render_version(tmpl.template_id, variables={"x": "v3"})
        versions = store.list_versions(tmpl.template_id)
        assert len(versions) == 3
        # Newest first
        assert versions[0].version == 3

    def test_render_nonexistent_template_raises(self, store: AgentTemplateStore) -> None:
        with pytest.raises(ValueError, match="not found"):
            store.render_version("nonexistent-id")

    def test_deterministic_hash(self, store: AgentTemplateStore) -> None:
        """Same variables → same hash."""
        tmpl = store.create_template(name="test", template_json={"system_prompt": "{{x}}"})
        v1 = store.render_version(tmpl.template_id, variables={"x": "same"})
        v2 = store.render_version(tmpl.template_id, variables={"x": "same"})
        assert v1.hash == v2.hash
        assert v1.version != v2.version  # different version numbers
