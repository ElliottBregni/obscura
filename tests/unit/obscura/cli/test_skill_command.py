from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from obscura.cli.commands import REPLContext, cmd_skill


def _make_ctx() -> REPLContext:
    return REPLContext(
        client=MagicMock(),
        store=MagicMock(),
        session_id="test-session",
        backend="copilot",
        model="gpt-5-mini",
        system_prompt="",
        max_turns=5,
        tools_enabled=True,
    )


def _write_skill(path: Path, name: str, body: str) -> None:
    path.write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n\n{body}\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_cmd_skill_load_and_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skills_dir = tmp_path / ".obscura" / "skills"
    skills_dir.mkdir(parents=True)
    _write_skill(skills_dir / "reviewer.md", "reviewer", "Focus on regressions.")

    monkeypatch.setattr(
        "obscura.cli.commands.resolve_obscura_skills_dir",
        lambda cwd=None: skills_dir,
    )

    ctx = _make_ctx()
    await cmd_skill("load reviewer", ctx)

    assert ctx.active_skills == ["reviewer"]
    injected = ctx.build_active_skill_context()
    assert "Skill: reviewer" in injected
    assert "Focus on regressions." in injected

    await cmd_skill("clear", ctx)
    assert ctx.active_skills == []


@pytest.mark.asyncio
async def test_cmd_skill_load_missing_reports_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skills_dir = tmp_path / ".obscura" / "skills"
    skills_dir.mkdir(parents=True)
    _write_skill(skills_dir / "builder.md", "builder", "Build things.")

    monkeypatch.setattr(
        "obscura.cli.commands.resolve_obscura_skills_dir",
        lambda cwd=None: skills_dir,
    )

    messages: list[str] = []
    monkeypatch.setattr(
        "obscura.cli.commands.print_error",
        lambda msg: messages.append(str(msg)),
    )

    ctx = _make_ctx()
    await cmd_skill("load not-a-skill", ctx)

    assert ctx.active_skills == []
    assert any("Skill not found: not-a-skill" in m for m in messages)
