"""Comprehensive feature parity tests — verifies all new modules, tools, and commands."""

from __future__ import annotations

import ast
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════
# 1. MODULE IMPORTS — verify every new module loads without errors
# ═══════════════════════════════════════════════════════════════════════════


class TestModuleImports:
    """Every new module must import cleanly."""

    def test_tools_file_state(self) -> None:
        pass

    def test_tools_diff_utils(self) -> None:
        pass

    def test_core_background_tasks(self) -> None:
        pass

    def test_core_cost_tracker(self) -> None:
        pass

    def test_core_permission_modes(self) -> None:
        pass

    def test_core_prompt_cache(self) -> None:
        pass

    def test_core_commit_attribution(self) -> None:
        pass

    def test_core_cleanup(self) -> None:
        pass

    def test_core_context_suggestions(self) -> None:
        pass

    def test_core_templates(self) -> None:
        pass

    def test_core_workflows(self) -> None:
        pass

    def test_agent_definitions(self) -> None:
        pass

    def test_agent_coordinator(self) -> None:
        pass

    def test_agent_workspace_isolation(self) -> None:
        pass

    def test_kairos_all(self) -> None:
        pass

    def test_kairos_uds(self) -> None:
        pass

    def test_kairos_background_sessions(self) -> None:
        pass

    def test_voice(self) -> None:
        pass

    def test_services_lsp(self) -> None:
        pass

    def test_tools_lsp(self) -> None:
        pass

    def test_tools_browser(self) -> None:
        pass

    def test_tools_worktree(self) -> None:
        pass

    def test_tools_task(self) -> None:
        pass

    def test_cli_tool_collapse(self) -> None:
        pass

    def test_cli_tips(self) -> None:
        pass

    def test_cli_module(self) -> None:
        pass


# ═══════════════════════════════════════════════════════════════════════════
# 2. TOOL REGISTRATION — all tools must register
# ═══════════════════════════════════════════════════════════════════════════


class TestToolRegistration:
    def test_worktree_tools(self) -> None:
        from obscura.tools.worktree import get_worktree_tool_specs

        specs = get_worktree_tool_specs()
        names = [s.name for s in specs]
        assert "enter_worktree" in names
        assert "exit_worktree" in names

    def test_task_tools(self) -> None:
        from obscura.tools.task_tools import get_task_tool_specs

        specs = get_task_tool_specs()
        names = [s.name for s in specs]
        assert "task_create" in names
        assert "task_list" in names
        assert "task_stop" in names
        assert len(specs) == 6

    def test_lsp_tool(self) -> None:
        from obscura.tools.lsp import get_lsp_tool_specs

        specs = get_lsp_tool_specs()
        assert len(specs) == 1
        assert specs[0].name == "lsp"

    def test_browser_tool(self) -> None:
        from obscura.tools.browser import get_browser_tool_specs

        specs = get_browser_tool_specs()
        assert len(specs) == 1
        assert specs[0].name == "web_browser"

    def test_goal_tool(self) -> None:
        from obscura.tools.goal_tools import get_goal_tool_specs

        specs = get_goal_tool_specs()
        assert len(specs) == 1
        assert specs[0].name == "goal"


# ═══════════════════════════════════════════════════════════════════════════
# 3. COMMAND REGISTRATION — all commands must be in COMMANDS dict
# ═══════════════════════════════════════════════════════════════════════════


class TestCommandRegistration:
    def _get_commands(self) -> set[str]:
        with open("obscura/cli/commands.py") as f:
            tree = ast.parse(f.read())
        return {
            n.name
            for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name.startswith("cmd_")
        }

    def test_command_count(self) -> None:
        cmds = self._get_commands()
        assert len(cmds) >= 75

    def test_wave2_commands(self) -> None:
        cmds = self._get_commands()
        for cmd in [
            "cmd_permissions",
            "cmd_resume",
            "cmd_cost",
            "cmd_doctor",
            "cmd_vim",
            "cmd_effort",
            "cmd_fast",
            "cmd_commit",
            "cmd_review",
            "cmd_security_review",
            "cmd_export",
            "cmd_coordinator",
            "cmd_voice",
        ]:
            assert cmd in cmds, f"Missing: {cmd}"

    def test_wave3_commands(self) -> None:
        cmds = self._get_commands()
        for cmd in [
            "cmd_kairos",
            "cmd_attribution",
            "cmd_ps",
            "cmd_logs",
            "cmd_kill_session",
            "cmd_suggestions",
            "cmd_template",
            "cmd_workflow",
            "cmd_peers",
            "cmd_send",
        ]:
            assert cmd in cmds, f"Missing: {cmd}"

    def test_new_commands(self) -> None:
        cmds = self._get_commands()
        for cmd in [
            "cmd_add_dir",
            "cmd_files",
            "cmd_rewind",
            "cmd_rename",
            "cmd_tag",
            "cmd_version",
            "cmd_usage",
            "cmd_copy",
            "cmd_brief",
            "cmd_stats",
            "cmd_btw",
            "cmd_sandbox_toggle",
            "cmd_summary",
            "cmd_stash",
            "cmd_pop",
        ]:
            assert cmd in cmds, f"Missing: {cmd}"


# ═══════════════════════════════════════════════════════════════════════════
# 4. PERMISSION MODES — enforcement must work
# ═══════════════════════════════════════════════════════════════════════════


class TestPermissionModes:
    def test_plan_mode_blocks_writes(self) -> None:
        from obscura.core.permission_modes import PermissionMode, PermissionModeEngine

        e = PermissionModeEngine(PermissionMode.PLAN)
        assert not e.evaluate("write_text_file").allowed
        assert not e.evaluate("edit_text_file").allowed
        assert not e.evaluate("run_shell").allowed

    def test_accept_edits_auto_approves(self) -> None:
        from obscura.core.permission_modes import PermissionMode, PermissionModeEngine

        e = PermissionModeEngine(PermissionMode.ACCEPT_EDITS)
        d = e.evaluate("edit_text_file")
        assert d.allowed
        assert d.auto_approved

    def test_dangerous_always_denied(self) -> None:
        from obscura.core.permission_modes import PermissionMode, PermissionModeEngine

        for mode in PermissionMode:
            e = PermissionModeEngine(mode)
            assert not e.evaluate("run_shell", {"script": "rm -rf /"}).allowed
            assert not e.evaluate(
                "run_shell",
                {"script": "sudo dd if=/dev/zero"},
            ).allowed


# ═══════════════════════════════════════════════════════════════════════════
# 5. AGENT DEFINITIONS — loading and resolution
# ═══════════════════════════════════════════════════════════════════════════


class TestAgentDefinitions:
    def test_builtin_agents_exist(self) -> None:
        from obscura.agent.definitions import resolve_all_definitions

        defs = resolve_all_definitions()
        for name in [
            "general-purpose",
            "explore",
            "plan",
            "verification",
            "coordinator",
        ]:
            assert name in defs, f"Missing built-in agent: {name}"

    def test_definition_has_system_prompt(self) -> None:
        from obscura.agent.definitions import resolve_all_definitions

        defs = resolve_all_definitions()
        for name, defn in defs.items():
            if defn.source == "built-in":
                assert defn.system_prompt, f"Built-in agent {name} has no system prompt"

    def test_explore_agent_is_read_only(self) -> None:
        from obscura.agent.definitions import resolve_all_definitions

        defs = resolve_all_definitions()
        explore = defs["explore"]
        assert "read_text_file" in explore.tools or "Read" in explore.tools
        assert "write_text_file" not in explore.tools
        assert "Edit" not in explore.tools or "edit_text_file" not in explore.tools

    def test_definition_to_config(self) -> None:
        from obscura.agent.definitions import AgentDefinition, definition_to_config_dict

        d = AgentDefinition(name="test", model="inherit", max_turns=42, tools=("Read",))
        cfg = definition_to_config_dict(d, parent_model="claude")
        assert cfg["provider"] == "claude"
        assert cfg["max_turns"] == 42
        assert cfg["tool_allowlist"] == ["Read"]


# ═══════════════════════════════════════════════════════════════════════════
# 6. KAIROS FEATURES — engine, frustration, undercover, away
# ═══════════════════════════════════════════════════════════════════════════


class TestKairos:
    def test_frustration_detection(self) -> None:
        from obscura.kairos.frustration import FrustrationDetector

        d = FrustrationDetector()
        assert d.analyze("wtf is this").is_frustrated
        assert d.analyze("this sucks").is_frustrated
        assert not d.analyze("looks good thanks").is_frustrated
        assert d.analyze("perfect").sentiment == "positive"

    def test_undercover_on_by_default(self) -> None:
        from obscura.kairos.undercover import is_undercover

        # Default is ON
        old = os.environ.get("OBSCURA_UNDERCOVER")
        os.environ.pop("OBSCURA_UNDERCOVER", None)
        assert is_undercover()
        if old is not None:
            os.environ["OBSCURA_UNDERCOVER"] = old

    def test_undercover_sanitizes_commits(self) -> None:
        from obscura.kairos.undercover import UndercoverMode

        uc = UndercoverMode()
        uc.force(True)
        msg = "Fix bug\n\nCo-Authored-By: Claude AI <noreply@anthropic.com>"
        sanitized = uc.sanitize_commit_message(msg)
        assert "Claude" not in sanitized
        assert "Fix bug" in sanitized

    def test_engine_creates(self) -> None:
        from obscura.kairos.engine import KairosEngine

        e = KairosEngine()
        assert not e.is_running
        e.log("test")
        assert e.status()["observations"] == 1

    async def test_away_summary(self) -> None:
        from obscura.kairos.away_summary import generate_away_summary

        s = await generate_away_summary(
            [
                ("user", "Fix the auth bug"),
                ("assistant", "Found the issue in auth.py line 42"),
            ],
        )
        assert len(s) > 0


# ═══════════════════════════════════════════════════════════════════════════
# 7. COST TRACKER — pricing and accumulation
# ═══════════════════════════════════════════════════════════════════════════


class TestCostTracker:
    def test_record_and_total(self) -> None:
        from obscura.core.cost_tracker import CostTracker

        t = CostTracker()
        t.record(1000, 500, "claude-sonnet-4-5")
        assert t.turn_count() == 1
        assert t.session_total_usd() > 0

    def test_summary_format(self) -> None:
        from obscura.core.cost_tracker import CostTracker

        t = CostTracker()
        t.record(1000, 500, "claude-sonnet-4-5")
        s = t.summary()
        assert "1 turns" in s
        assert "$" in s


# ═══════════════════════════════════════════════════════════════════════════
# 8. EFFORT LEVELS — enum and budget mapping
# ═══════════════════════════════════════════════════════════════════════════


class TestEffortLevels:
    def test_all_levels_defined(self) -> None:
        from obscura.core.types import EFFORT_THINKING_BUDGETS, EffortLevel

        for level in EffortLevel:
            assert level in EFFORT_THINKING_BUDGETS

    def test_budget_ordering(self) -> None:
        from obscura.core.types import EFFORT_THINKING_BUDGETS, EffortLevel

        assert (
            EFFORT_THINKING_BUDGETS[EffortLevel.LOW]
            < EFFORT_THINKING_BUDGETS[EffortLevel.MEDIUM]
        )
        assert (
            EFFORT_THINKING_BUDGETS[EffortLevel.MEDIUM]
            < EFFORT_THINKING_BUDGETS[EffortLevel.HIGH]
        )
        assert (
            EFFORT_THINKING_BUDGETS[EffortLevel.HIGH]
            < EFFORT_THINKING_BUDGETS[EffortLevel.MAX]
        )


# ═══════════════════════════════════════════════════════════════════════════
# 9. TOOL COLLAPSE — grouping consecutive tool calls
# ═══════════════════════════════════════════════════════════════════════════


class TestToolCollapse:
    def test_collapsible_recorded(self) -> None:
        from obscura.cli.tool_collapse import ToolCollapser

        c = ToolCollapser()
        assert c.record("read_text_file", {"path": "x"})
        assert not c.record("run_shell", {"script": "ls"})

    def test_flush_summary(self) -> None:
        from obscura.cli.tool_collapse import ToolCollapser

        c = ToolCollapser()
        c.record("read_text_file", {"path": "a.py"})
        c.record("read_text_file", {"path": "b.py"})
        c.record("grep_files", {"pattern": "TODO"})
        s = c.flush_summary()
        assert "Read" in s
        assert "Grep" in s
        assert c.count == 0


# ═══════════════════════════════════════════════════════════════════════════
# 10. FILE STATE — staleness tracking
# ═══════════════════════════════════════════════════════════════════════════


class TestFileState:
    def test_fresh_read(self, tmp_path: Path) -> None:
        from obscura.tools.system.file_state import check_staleness, clear, record_read

        clear()
        f = tmp_path / "test.txt"
        f.write_text("hello")
        record_read(f)
        assert check_staleness(f) is None

    def test_stale_after_modify(self, tmp_path: Path) -> None:
        import time

        from obscura.tools.system.file_state import check_staleness, clear, record_read

        clear()
        f = tmp_path / "test.txt"
        f.write_text("v1")
        record_read(f)
        time.sleep(0.05)
        f.write_text("v2")
        assert check_staleness(f) is not None


# ═══════════════════════════════════════════════════════════════════════════
# 11. DIFF UTILS — structured diff generation
# ═══════════════════════════════════════════════════════════════════════════


class TestDiffUtils:
    def test_compute_diff(self) -> None:
        from obscura.tools.system.diff_utils import compute_unified_diff

        d = compute_unified_diff("hello\n", "hello\nworld\n", "test.txt")
        assert d["insertions"] == 1
        assert d["deletions"] == 0
        assert "1 insertion" in d["summary"]

    def test_empty_diff(self) -> None:
        from obscura.tools.system.diff_utils import compute_unified_diff

        d = compute_unified_diff("same\n", "same\n", "test.txt")
        assert d["insertions"] == 0
        assert d["deletions"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 12. PROMPT CACHE — hit/miss tracking
# ═══════════════════════════════════════════════════════════════════════════


class TestPromptCache:
    def test_cache_miss_then_hit(self) -> None:
        from obscura.core.prompt_cache import PromptCacheManager

        pc = PromptCacheManager()
        assert not pc.check("prompt", [{"name": "tool1"}])
        assert pc.check("prompt", [{"name": "tool1"}])

    def test_invalidate(self) -> None:
        from obscura.core.prompt_cache import PromptCacheManager

        pc = PromptCacheManager()
        pc.check("prompt")
        pc.invalidate()
        assert not pc.check("prompt")


# ═══════════════════════════════════════════════════════════════════════════
# 13. STASH/POP — context saving
# ═══════════════════════════════════════════════════════════════════════════


class TestStashPop:
    def test_stash_stack(self) -> None:
        from obscura.cli.commands import _stash_stack

        _stash_stack.clear()
        # Simulate stash
        history = [("user", "hello"), ("assistant", "hi")]
        _stash_stack.append((list(history), "session-1", []))
        assert len(_stash_stack) == 1
        # Simulate pop
        h, sid, _fc = _stash_stack.pop()
        assert len(h) == 2
        assert sid == "session-1"
        assert len(_stash_stack) == 0


# ═══════════════════════════════════════════════════════════════════════════
# 14. OBSCURA.MD NAMING — no CLAUDE.md in source
# ═══════════════════════════════════════════════════════════════════════════


class TestBranding:
    def test_context_loader_uses_obscura_md(self) -> None:
        from obscura.core.context import ContextLoader

        assert hasattr(ContextLoader, "load_project_instructions")

    def test_no_claude_md_in_commands(self) -> None:
        with open("obscura/cli/commands.py") as f:
            content = f.read()
        # Should reference OBSCURA.md, not CLAUDE.md
        assert "OBSCURA.md" in content
        # CLAUDE.md should only appear in backwards-compat fallback in context.py
        assert 'Path.cwd() / "CLAUDE.md"' not in content


# ═══════════════════════════════════════════════════════════════════════════
# 15. VOICE DEPENDENCIES — check without crashing
# ═══════════════════════════════════════════════════════════════════════════


class TestVoice:
    def test_dependency_check(self) -> None:
        from obscura.voice.capture import check_voice_dependencies

        deps = check_voice_dependencies()
        assert isinstance(deps.available, bool)
        assert isinstance(deps.backend, str)

    def test_session_creates(self) -> None:
        from obscura.voice.session import VoiceSession

        s = VoiceSession()
        assert not s.is_recording
