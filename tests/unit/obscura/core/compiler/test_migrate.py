"""Tests for obscura.core.compiler.migrate — agents.yaml migration."""

from __future__ import annotations

from pathlib import Path

import yaml

from obscura.core.compiler.migrate import MigrationResult, migrate_agents_yaml


def _write_agents_yaml(path: Path, agents: list[dict]) -> None:
    path.write_text(
        yaml.dump({"agents": agents}, default_flow_style=False),
        encoding="utf-8",
    )


class TestMigrateAgentsYaml:
    def test_basic_migration(self, tmp_path: Path) -> None:
        agents_yaml = tmp_path / "agents.yaml"
        output_dir = tmp_path / "templates"

        _write_agents_yaml(agents_yaml, [
            {
                "name": "dev",
                "type": "loop",
                "provider": "claude",
                "max_turns": 30,
                "system_prompt": "You are a developer.",
                "capabilities": {"grant": ["shell.exec", "file.read"], "deny": []},
                "plugins": {"require": ["git"], "optional": ["shell"]},
            },
        ])

        result = migrate_agents_yaml(agents_yaml, output_dir)

        assert "dev" in result.templates_written
        assert not result.errors

        generated = output_dir / "dev.yml"
        assert generated.is_file()

        doc = yaml.safe_load(generated.read_text())
        assert doc["kind"] == "Template"
        assert doc["metadata"]["name"] == "dev"
        assert doc["spec"]["provider"] == "claude"
        assert doc["spec"]["max_iterations"] == 30
        assert "shell.exec" in doc["spec"]["capabilities"]
        assert "git" in doc["spec"]["plugins"]
        assert "shell" in doc["spec"]["plugins"]

    def test_missing_file(self, tmp_path: Path) -> None:
        result = migrate_agents_yaml(
            tmp_path / "nonexistent.yaml",
            tmp_path / "out",
        )
        assert len(result.errors) == 1
        assert "not found" in result.errors[0]

    def test_disabled_agents_skipped(self, tmp_path: Path) -> None:
        agents_yaml = tmp_path / "agents.yaml"
        _write_agents_yaml(agents_yaml, [
            {"name": "active", "type": "loop", "provider": "copilot"},
            {"name": "disabled", "type": "loop", "provider": "copilot", "enabled": False},
        ])

        result = migrate_agents_yaml(agents_yaml, tmp_path / "out")
        assert "active" in result.templates_written
        assert any("disabled" in s for s in result.skipped)

    def test_no_overwrite_by_default(self, tmp_path: Path) -> None:
        agents_yaml = tmp_path / "agents.yaml"
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        _write_agents_yaml(agents_yaml, [
            {"name": "existing", "type": "loop", "provider": "copilot"},
        ])

        # Create pre-existing file
        (output_dir / "existing.yml").write_text("# existing", encoding="utf-8")

        result = migrate_agents_yaml(agents_yaml, output_dir)
        assert any("existing" in s for s in result.skipped)
        # File should NOT be overwritten
        assert (output_dir / "existing.yml").read_text() == "# existing"

    def test_overwrite_flag(self, tmp_path: Path) -> None:
        agents_yaml = tmp_path / "agents.yaml"
        output_dir = tmp_path / "out"
        output_dir.mkdir()

        _write_agents_yaml(agents_yaml, [
            {"name": "overme", "type": "loop", "provider": "copilot"},
        ])

        (output_dir / "overme.yml").write_text("# old", encoding="utf-8")

        result = migrate_agents_yaml(agents_yaml, output_dir, overwrite=True)
        assert "overme" in result.templates_written

        doc = yaml.safe_load((output_dir / "overme.yml").read_text())
        assert doc["kind"] == "Template"

    def test_tags_preserved(self, tmp_path: Path) -> None:
        agents_yaml = tmp_path / "agents.yaml"
        _write_agents_yaml(agents_yaml, [
            {"name": "tagged", "type": "loop", "provider": "copilot", "tags": ["dev", "qa"]},
        ])

        result = migrate_agents_yaml(agents_yaml, tmp_path / "out")
        assert "tagged" in result.templates_written

        doc = yaml.safe_load((tmp_path / "out" / "tagged.yml").read_text())
        assert doc["metadata"]["tags"] == ["dev", "qa"]

    def test_config_fields_mapped(self, tmp_path: Path) -> None:
        agents_yaml = tmp_path / "agents.yaml"
        _write_agents_yaml(agents_yaml, [
            {
                "name": "full",
                "type": "loop",
                "provider": "claude",
                "model_id": "sonnet-4",
                "timeout_seconds": 300,
                "memory_namespace": "test",
                "can_delegate": True,
                "delegate_allowlist": ["other"],
                "max_delegation_depth": 2,
            },
        ])

        result = migrate_agents_yaml(agents_yaml, tmp_path / "out")
        assert "full" in result.templates_written

        doc = yaml.safe_load((tmp_path / "out" / "full.yml").read_text())
        assert doc["spec"]["model_id"] == "sonnet-4"
        assert doc["spec"]["config"]["timeout_seconds"] == 300
        assert doc["spec"]["config"]["memory_namespace"] == "test"
        assert doc["spec"]["config"]["can_delegate"] is True

    def test_permissions_mapped_to_tools(self, tmp_path: Path) -> None:
        agents_yaml = tmp_path / "agents.yaml"
        _write_agents_yaml(agents_yaml, [
            {
                "name": "perms",
                "type": "loop",
                "provider": "copilot",
                "permissions": {
                    "allow": ["read_file", "write_file"],
                    "deny": ["delete_file"],
                },
            },
        ])

        result = migrate_agents_yaml(agents_yaml, tmp_path / "out")
        doc = yaml.safe_load((tmp_path / "out" / "perms.yml").read_text())
        assert doc["spec"]["tool_allowlist"] == ["read_file", "write_file"]
        assert doc["spec"]["tool_denylist"] == ["delete_file"]

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        agents_yaml = tmp_path / "agents.yaml"
        agents_yaml.write_text("{{invalid yaml", encoding="utf-8")

        result = migrate_agents_yaml(agents_yaml, tmp_path / "out")
        assert len(result.errors) == 1

    def test_missing_agents_key(self, tmp_path: Path) -> None:
        agents_yaml = tmp_path / "agents.yaml"
        agents_yaml.write_text("foo: bar\n", encoding="utf-8")

        result = migrate_agents_yaml(agents_yaml, tmp_path / "out")
        assert len(result.errors) == 1
        assert "agents" in result.errors[0]

    def test_multiple_agents(self, tmp_path: Path) -> None:
        agents_yaml = tmp_path / "agents.yaml"
        _write_agents_yaml(agents_yaml, [
            {"name": "a1", "type": "loop", "provider": "copilot"},
            {"name": "a2", "type": "loop", "provider": "claude"},
            {"name": "a3", "type": "loop", "provider": "copilot"},
        ])

        result = migrate_agents_yaml(agents_yaml, tmp_path / "out")
        assert len(result.templates_written) == 3
        assert (tmp_path / "out" / "a1.yml").is_file()
        assert (tmp_path / "out" / "a2.yml").is_file()
        assert (tmp_path / "out" / "a3.yml").is_file()
