"""Integration tests for loading skills and MCP configs from .obscura."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from obscura.integrations.mcp.config_loader import discover_mcp_servers
from obscura.skills.docs_loader import load_markdown_skill_documents


@pytest.mark.integration
def test_loads_mcp_from_workspace_obscura_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    mcp_dir = workspace / ".obscura" / "mcp"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "servers.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "supabase": {
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "@supabase/mcp-server"],
                        "env": {
                            "SUPABASE_ACCESS_TOKEN": "${SUPABASE_ACCESS_TOKEN}"
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("SUPABASE_ACCESS_TOKEN", "sb-test")
    monkeypatch.delenv("OBSCURA_HOME", raising=False)

    discovered = discover_mcp_servers()
    assert len(discovered) == 1
    assert discovered[0].name == "supabase"
    assert discovered[0].env["SUPABASE_ACCESS_TOKEN"] == "sb-test"


@pytest.mark.integration
def test_loads_skill_documents_from_workspace_obscura_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    skills_dir = workspace / ".obscura" / "skills"
    (skills_dir / "roles" / "reviewer").mkdir(parents=True)
    (skills_dir / "python.md").write_text("# Python\nUse strict typing.", encoding="utf-8")
    (skills_dir / "roles" / "reviewer" / "style.md").write_text(
        "# Reviewer Style\nFocus on regressions.",
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("OBSCURA_HOME", raising=False)

    docs = load_markdown_skill_documents()
    names = [doc.name for doc in docs]
    assert "python" in names
    assert "roles/reviewer/style" in names


@pytest.mark.integration
def test_obscura_home_env_overrides_workspace_obscura(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    workspace_skills = workspace / ".obscura" / "skills"
    workspace_skills.mkdir(parents=True)
    (workspace_skills / "workspace.md").write_text("# Workspace", encoding="utf-8")

    env_home = tmp_path / "env-obscura"
    env_skills = env_home / "skills"
    env_skills.mkdir(parents=True)
    (env_skills / "env.md").write_text("# Env", encoding="utf-8")

    monkeypatch.chdir(workspace)
    monkeypatch.setenv("OBSCURA_HOME", str(env_home))

    docs = load_markdown_skill_documents()
    names = {doc.name for doc in docs}
    assert "env" in names
    assert "workspace" not in names
