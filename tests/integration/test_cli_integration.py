"""Integration tests — simulate the REPL flow with mocked backends to catch runtime bugs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Never
from unittest.mock import AsyncMock, MagicMock

import pytest

# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _make_mock_ctx(**overrides: Any) -> Any:
    """Build a minimal mock REPLContext for command testing."""
    from obscura.cli.commands import REPLContext

    # Build kwargs with proper defaults for all fields.
    kwargs: dict[str, Any] = {
        "client": MagicMock(),
        "store": MagicMock(),
        "session_id": "test-session-001",
        "backend": "copilot",
        "model": "test-model",
        "system_prompt": "You are a test agent.",
        "max_turns": 10,
        "tools_enabled": True,
        "mcp_configs": [],
        "confirm_enabled": False,
        "confirm_always": set(),
        "message_history": [],
        "_file_changes": [],
        "_pending_file_reads": {},
        "_mode_manager": None,
        "vector_store": None,
        "_context_router": None,
        "_turn_classifier": None,
        "_runtime": None,
        "_swarm_runs": {},
        "_supervisor": None,
        "_supervisor_task": None,
        "_lazy_skill_loader": None,
        "active_skills": [],
        "_lazy_command_loader": None,
        "_permission_mode": "default",
        "_effort_level": "medium",
        "_voice_enabled": False,
        "_collapser": None,
    }
    kwargs.update(overrides)
    return REPLContext(**kwargs)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Command execution tests — do commands run without crashing?
# ═══════════════════════════════════════════════════════════════════════════


class TestCommandExecution:
    """Test that every new command executes without crashing."""

    async def test_cmd_version(self) -> None:
        from obscura.cli.commands import cmd_version

        ctx = _make_mock_ctx()
        result = await cmd_version("", ctx)
        assert result is None  # Should not crash

    async def test_cmd_stats(self) -> None:
        from obscura.cli.commands import cmd_stats

        ctx = _make_mock_ctx()
        result = await cmd_stats("", ctx)
        assert result is None

    async def test_cmd_cost_empty(self) -> None:
        from obscura.cli.commands import cmd_cost

        ctx = _make_mock_ctx()
        result = await cmd_cost("", ctx)
        assert result is None

    async def test_cmd_cost_with_data(self) -> None:
        from obscura.cli.commands import cmd_cost
        from obscura.core.cost_tracker import get_cost_tracker

        tracker = get_cost_tracker()
        tracker.reset()
        tracker.record(1000, 500, "test-model")
        ctx = _make_mock_ctx()
        result = await cmd_cost("", ctx)
        assert result is None

    async def test_cmd_effort_set(self) -> None:
        from obscura.cli.commands import cmd_effort

        ctx = _make_mock_ctx()
        await cmd_effort("high", ctx)
        assert ctx._effort_level == "high"

    async def test_cmd_effort_max_shows_banner(self) -> None:
        from obscura.cli.commands import cmd_effort

        ctx = _make_mock_ctx()
        # Should not crash even with banner display.
        await cmd_effort("max", ctx)
        assert ctx._effort_level == "max"

    async def test_cmd_fast_toggle(self) -> None:
        from obscura.cli.commands import cmd_fast

        ctx = _make_mock_ctx()
        await cmd_fast("", ctx)
        assert ctx._effort_level == "low"
        await cmd_fast("", ctx)
        assert ctx._effort_level == "medium"

    async def test_cmd_permissions_show(self) -> None:
        from obscura.cli.commands import cmd_permissions

        ctx = _make_mock_ctx()
        result = await cmd_permissions("", ctx)
        assert result is None

    async def test_cmd_permissions_set(self) -> None:
        from obscura.cli.commands import cmd_permissions

        ctx = _make_mock_ctx()
        await cmd_permissions("plan", ctx)
        assert ctx._permission_mode == "plan"

    async def test_cmd_permissions_invalid(self) -> None:
        from obscura.cli.commands import cmd_permissions

        ctx = _make_mock_ctx()
        await cmd_permissions("nonexistent", ctx)
        # Should print error but not crash.

    async def test_cmd_doctor(self) -> None:
        from obscura.cli.commands import cmd_doctor

        ctx = _make_mock_ctx()
        result = await cmd_doctor("", ctx)
        assert result is None

    async def test_cmd_vim(self) -> None:
        from obscura.cli.commands import cmd_vim

        ctx = _make_mock_ctx()
        await cmd_vim("", ctx)
        assert ctx._vim_mode is True  # type: ignore[attr-defined]
        await cmd_vim("", ctx)
        assert ctx._vim_mode is False  # type: ignore[attr-defined]

    async def test_cmd_files_empty(self) -> None:
        from obscura.cli.commands import cmd_files
        from obscura.tools.system.file_state import clear

        clear()
        ctx = _make_mock_ctx()
        result = await cmd_files("", ctx)
        assert result is None

    async def test_cmd_export_md(self) -> None:
        from obscura.cli.commands import cmd_export

        ctx = _make_mock_ctx()
        ctx.message_history = [("user", "hello"), ("assistant", "hi")]
        result = await cmd_export("md", ctx)
        assert result is None
        # Check file was created.
        export_path = Path.home() / ".obscura" / "exports" / f"{ctx.session_id[:12]}.md"
        assert export_path.exists()
        content = export_path.read_text()
        assert "hello" in content
        export_path.unlink(missing_ok=True)

    async def test_cmd_export_json(self) -> None:
        from obscura.cli.commands import cmd_export

        ctx = _make_mock_ctx()
        ctx.message_history = [("user", "test"), ("assistant", "response")]
        await cmd_export("json", ctx)
        export_path = (
            Path.home() / ".obscura" / "exports" / f"{ctx.session_id[:12]}.json"
        )
        assert export_path.exists()
        data = json.loads(export_path.read_text())
        assert len(data) == 2
        export_path.unlink(missing_ok=True)

    async def test_cmd_kairos_status(self) -> None:
        from obscura.cli.commands import cmd_kairos

        ctx = _make_mock_ctx()
        result = await cmd_kairos("status", ctx)
        assert result is None

    async def test_cmd_coordinator_toggle(self) -> None:
        from obscura.cli.commands import cmd_coordinator

        ctx = _make_mock_ctx()
        await cmd_coordinator("on", ctx)
        from obscura.agent.coordinator import is_coordinator_mode

        assert is_coordinator_mode()
        await cmd_coordinator("off", ctx)
        assert not is_coordinator_mode()

    async def test_cmd_voice_no_deps(self) -> None:
        from obscura.cli.commands import cmd_voice

        ctx = _make_mock_ctx()
        # Should not crash even without SoX.
        result = await cmd_voice("on", ctx)
        assert result is None

    async def test_cmd_stash_pop(self) -> None:
        from obscura.cli.commands import _stash_stack, cmd_pop, cmd_stash

        _stash_stack.clear()
        ctx = _make_mock_ctx()
        ctx.message_history = [("user", "hello"), ("assistant", "hi")]

        await cmd_stash("", ctx)
        assert len(ctx.message_history) == 0  # cleared
        assert len(_stash_stack) == 1

        await cmd_pop("", ctx)
        assert len(ctx.message_history) == 2  # restored
        assert len(_stash_stack) == 0

    async def test_cmd_pop_empty(self) -> None:
        from obscura.cli.commands import _stash_stack, cmd_pop

        _stash_stack.clear()
        ctx = _make_mock_ctx()
        result = await cmd_pop("", ctx)
        assert result is None  # Should not crash

    async def test_cmd_sandbox_toggle(self) -> None:
        from obscura.cli.commands import cmd_sandbox_toggle

        ctx = _make_mock_ctx()
        old = os.environ.get("OBSCURA_SYSTEM_TOOLS_UNSAFE_FULL_ACCESS", "")
        await cmd_sandbox_toggle("", ctx)
        await cmd_sandbox_toggle("", ctx)  # toggle back
        os.environ["OBSCURA_SYSTEM_TOOLS_UNSAFE_FULL_ACCESS"] = old

    async def test_cmd_add_dir_invalid(self) -> None:
        from obscura.cli.commands import cmd_add_dir

        ctx = _make_mock_ctx()
        result = await cmd_add_dir("/nonexistent/path/xyz", ctx)
        assert result is None  # prints error, doesn't crash

    async def test_cmd_rewind_empty(self) -> None:
        from obscura.cli.commands import cmd_rewind

        ctx = _make_mock_ctx()
        result = await cmd_rewind("", ctx)
        assert result is None

    async def test_cmd_rewind_with_changes(self, tmp_path: Path) -> None:
        from obscura.cli.commands import cmd_rewind

        ctx = _make_mock_ctx()
        f = tmp_path / "test.py"
        f.write_text("original")
        ctx._file_changes = [
            {"path": str(f), "original": "original", "modified": "changed"},
        ]
        f.write_text("changed")
        await cmd_rewind("", ctx)
        assert f.read_text() == "original"
        assert len(ctx._file_changes) == 0

    async def test_cmd_rename(self) -> None:
        from obscura.cli.commands import cmd_rename

        ctx = _make_mock_ctx()
        ctx.store.update_summary = AsyncMock()
        result = await cmd_rename("My Session Title", ctx)
        assert result is None

    async def test_cmd_tag(self) -> None:
        from obscura.cli.commands import cmd_tag

        ctx = _make_mock_ctx()
        ctx.store.get_session = AsyncMock(return_value=MagicMock(metadata={"tags": []}))
        result = await cmd_tag("important", ctx)
        assert result is None

    async def test_cmd_brief(self) -> None:
        from obscura.cli.commands import cmd_brief

        ctx = _make_mock_ctx()
        await cmd_brief("", ctx)
        assert ctx._effort_level == "low"
        await cmd_brief("", ctx)
        assert ctx._effort_level == "medium"

    async def test_cmd_usage_empty(self) -> None:
        from obscura.cli.commands import cmd_usage
        from obscura.core.cost_tracker import get_cost_tracker

        get_cost_tracker().reset()
        ctx = _make_mock_ctx()
        result = await cmd_usage("", ctx)
        assert result is None

    async def test_cmd_template_list(self) -> None:
        from obscura.cli.commands import cmd_template

        ctx = _make_mock_ctx()
        result = await cmd_template("list", ctx)
        assert result is None

    async def test_cmd_workflow_list(self) -> None:
        from obscura.cli.commands import cmd_workflow

        ctx = _make_mock_ctx()
        result = await cmd_workflow("list", ctx)
        assert result is None

    async def test_cmd_ps(self) -> None:
        from obscura.cli.commands import cmd_ps

        ctx = _make_mock_ctx()
        result = await cmd_ps("", ctx)
        assert result is None

    async def test_cmd_attribution_empty(self) -> None:
        from obscura.cli.commands import cmd_attribution

        ctx = _make_mock_ctx()
        result = await cmd_attribution("", ctx)
        assert result is None

    async def test_cmd_suggestions_empty(self) -> None:
        from obscura.cli.commands import cmd_suggestions
        from obscura.tools.system.file_state import clear

        clear()
        ctx = _make_mock_ctx()
        result = await cmd_suggestions("", ctx)
        assert result is None

    async def test_cmd_log_stats(self) -> None:
        from obscura.cli.commands import cmd_log

        ctx = _make_mock_ctx()
        result = await cmd_log("stats", ctx)
        assert result is None

    async def test_cmd_log_path(self) -> None:
        from obscura.cli.commands import cmd_log

        ctx = _make_mock_ctx()
        result = await cmd_log("path", ctx)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# 2. Tool execution tests — do tools return valid JSON?
# ═══════════════════════════════════════════════════════════════════════════


class TestToolExecution:
    """Test that tools return valid JSON and don't crash."""

    async def test_sleep_tool(self) -> None:
        from obscura.tools.system import get_system_tool_specs

        specs = {s.name: s for s in get_system_tool_specs()}
        result = await specs["sleep"].handler(seconds=0.01)
        data = json.loads(result)
        assert data["ok"] is True
        assert data["slept_seconds"] == 0.01

    async def test_config_list(self) -> None:
        from obscura.tools.system import get_system_tool_specs

        specs = {s.name: s for s in get_system_tool_specs()}
        result = await specs["config"].handler(action="list")
        data = json.loads(result)
        assert data["ok"] is True
        assert "settings" in data

    async def test_config_get_missing(self) -> None:
        from obscura.tools.system import get_system_tool_specs

        specs = {s.name: s for s in get_system_tool_specs()}
        result = await specs["config"].handler(action="get", key="nonexistent.key")
        data = json.loads(result)
        assert data["ok"] is True
        assert data["found"] is False

    async def test_tool_search_empty(self) -> None:
        from obscura.tools.system import get_system_tool_specs

        specs = {s.name: s for s in get_system_tool_specs()}
        result = await specs["tool_search"].handler(query="nonexistenttool123")
        data = json.loads(result)
        # ok=False when no registry is set (module-level _tool_registry_ref is None).
        assert "ok" in data

    async def test_context_window_status(self) -> None:
        from obscura.tools.system import get_system_tool_specs

        specs = {s.name: s for s in get_system_tool_specs()}
        result = await specs["context_window_status"].handler()
        data = json.loads(result)
        assert data["ok"] is True

    async def test_read_text_file(self, tmp_path: Path) -> None:
        from obscura.tools.system import get_system_tool_specs
        from obscura.tools.system.file_state import clear

        clear()
        specs = {s.name: s for s in get_system_tool_specs()}
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        result = await specs["read_text_file"].handler(path=str(f))
        data = json.loads(result)
        assert data["ok"] is True
        assert "hello world" in data["text"]

    async def test_read_image_file(self, tmp_path: Path) -> None:
        from obscura.tools.system import get_system_tool_specs
        from obscura.tools.system.file_state import clear

        clear()
        specs = {s.name: s for s in get_system_tool_specs()}
        f = tmp_path / "test.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        result = await specs["read_text_file"].handler(path=str(f))
        data = json.loads(result)
        assert data["ok"] is True
        assert data["kind"] == "image"
        assert "base64" in data

    async def test_edit_with_diff_output(self, tmp_path: Path) -> None:
        from obscura.tools.system import get_system_tool_specs
        from obscura.tools.system.file_state import clear, record_read

        clear()
        specs = {s.name: s for s in get_system_tool_specs()}
        f = tmp_path / "test.py"
        f.write_text("hello\nworld\n")
        record_read(f)
        result = await specs["edit_text_file"].handler(
            path=str(f),
            old_text="world",
            new_text="planet",
        )
        data = json.loads(result)
        assert data["ok"] is True
        assert "diff" in data
        assert data["diff"]["insertions"] >= 1

    async def test_grep_ripgrep(self, tmp_path: Path) -> None:
        from obscura.tools.system import get_system_tool_specs

        specs = {s.name: s for s in get_system_tool_specs()}
        f = tmp_path / "test.py"
        f.write_text("def hello():\n    pass\n")
        result = await specs["grep_files"].handler(pattern="hello", path=str(tmp_path))
        data = json.loads(result)
        assert data["ok"] is True
        assert data["count"] >= 1

    async def test_grep_output_modes(self, tmp_path: Path) -> None:
        from obscura.tools.system import get_system_tool_specs

        specs = {s.name: s for s in get_system_tool_specs()}
        f = tmp_path / "test.py"
        f.write_text("line1\nline2\nline1\n")

        for mode in ["content", "files_with_matches", "count"]:
            result = await specs["grep_files"].handler(
                pattern="line1",
                path=str(tmp_path),
                output_mode=mode,
            )
            data = json.loads(result)
            assert data["ok"] is True, f"mode={mode} failed"

    async def test_task_create_and_list(self) -> None:
        from obscura.tools.task_tools import get_task_tool_specs

        specs = {s.name: s for s in get_task_tool_specs()}

        # Create.
        result = await specs["task_create"].handler(
            subject="Test task",
            description="Testing",
        )
        data = json.loads(result)
        assert data["ok"] is True
        task_id = data["task_id"]

        # List.
        result = await specs["task_list"].handler()
        data = json.loads(result)
        assert data["ok"] is True
        assert any(t["task_id"] == task_id for t in data["tasks"])

        # Get.
        result = await specs["task_get"].handler(task_id=task_id)
        data = json.loads(result)
        assert data["ok"] is True
        assert data["task"]["subject"] == "Test task"

        # Update.
        result = await specs["task_update"].handler(task_id=task_id, status="completed")
        data = json.loads(result)
        assert data["ok"] is True

        # Delete.
        result = await specs["task_update"].handler(task_id=task_id, status="deleted")
        data = json.loads(result)
        assert data["ok"] is True

    async def test_notebook_edit_nonexistent(self) -> None:
        from obscura.tools.system import get_system_tool_specs

        specs = {s.name: s for s in get_system_tool_specs()}
        result = await specs["notebook_edit"].handler(
            notebook_path="/nonexistent.ipynb",
            cell_index=0,
        )
        data = json.loads(result)
        assert data["ok"] is False

    async def test_history_snip_no_history(self) -> None:
        from obscura.tools.system import get_system_tool_specs

        specs = {s.name: s for s in get_system_tool_specs()}
        result = await specs["history_snip"].handler(start_turn=0, end_turn=0)
        data = json.loads(result)
        assert data["ok"] is False  # no history set


# ═══════════════════════════════════════════════════════════════════════════
# 3. Integration: permission modes actually block tools
# ═══════════════════════════════════════════════════════════════════════════


class TestPermissionIntegration:
    """Test that permission modes actually affect tool execution flow."""

    def test_plan_mode_blocks_in_evaluate(self) -> None:
        from obscura.core.permission_modes import PermissionMode, PermissionModeEngine

        engine = PermissionModeEngine(PermissionMode.PLAN)

        # These should be blocked.
        for tool in [
            "write_text_file",
            "edit_text_file",
            "run_shell",
            "run_command",
            "remove_path",
            "move_path",
            "git_commit",
        ]:
            d = engine.evaluate(tool)
            assert not d.allowed, f"{tool} should be blocked in PLAN mode"

        # These should be allowed.
        for tool in [
            "read_text_file",
            "grep_files",
            "find_files",
            "git_status",
            "web_search",
        ]:
            d = engine.evaluate(tool)
            assert d.allowed, f"{tool} should be allowed in PLAN mode"

    def test_dangerous_overrides_bypass(self) -> None:
        from obscura.core.permission_modes import PermissionMode, PermissionModeEngine

        engine = PermissionModeEngine(PermissionMode.BYPASS)

        dangerous_commands = [
            ("run_shell", {"script": "rm -rf /"}),
            ("run_shell", {"script": "sudo rm -rf /home"}),
            ("run_shell", {"script": "git push --force main"}),
            ("run_shell", {"script": "eval(user_input)"}),
            ("run_shell", {"script": "kubectl delete namespace production"}),
        ]
        for tool, args in dangerous_commands:
            d = engine.evaluate(tool, args)
            assert not d.allowed, (
                f"Dangerous command should be blocked even in BYPASS: {args}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# 4. Integration: deep log writes entries
# ═══════════════════════════════════════════════════════════════════════════


class TestDeepLogIntegration:
    def test_log_entries_written(self) -> None:
        from obscura.core.deep_log import DeepLogger

        log = DeepLogger(enabled=True)
        log.tool_call("test_tool", {"key": "val"}, duration_ms=10, ok=True)
        log.api_request("test-model", input_tokens=100, output_tokens=50)
        log.event("test_event", detail="hello")
        log.error("test error", source="test")
        log.flush()
        assert log.total_entries == 4
        log.close()

    def test_log_disabled(self) -> None:
        from obscura.core.deep_log import DeepLogger

        log = DeepLogger(enabled=False)
        log.tool_call("test_tool", {})
        log.flush()
        assert log.total_entries == 0


# ═══════════════════════════════════════════════════════════════════════════
# 5. Integration: session utilities
# ═══════════════════════════════════════════════════════════════════════════


class TestSessionUtilsIntegration:
    def test_register_and_list(self) -> None:
        from obscura.core.session_utils import (
            list_active_sessions,
            register_session,
            unregister_session,
        )

        sid = "test-integration-001"
        register_session(sid, backend="test")
        sessions = list_active_sessions()
        found = [s for s in sessions if s.get("session_id") == sid]
        assert len(found) == 1
        unregister_session(sid)
        sessions = list_active_sessions()
        found = [s for s in sessions if s.get("session_id") == sid]
        assert len(found) == 0

    def test_concurrent_detection(self) -> None:
        from obscura.core.session_utils import (
            check_concurrent_sessions,
            register_session,
            unregister_session,
        )

        # Use IDs long enough that 16-char prefix doesn't collide.
        sid1 = "concurrent-aaaa-1111-session"
        sid2 = "concurrent-bbbb-2222-session"
        register_session(sid1)
        register_session(sid2)
        concurrent = check_concurrent_sessions(sid1)
        # sid2 should appear as a concurrent session.
        assert len(concurrent) >= 1
        unregister_session(sid1)
        unregister_session(sid2)


# ═══════════════════════════════════════════════════════════════════════════
# 6. Integration: smart retry
# ═══════════════════════════════════════════════════════════════════════════


class TestSmartRetryIntegration:
    async def test_retry_on_server_error(self) -> None:
        from obscura.core.smart_retry import with_smart_retry

        call_count = 0

        async def flaky() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                msg = "500 Internal Server Error"
                raise RuntimeError(msg)
            return "success"

        result = await with_smart_retry(flaky, max_retries=3, initial_backoff=0.01)
        assert result == "success"
        assert call_count == 3

    async def test_no_retry_on_non_retryable(self) -> None:
        from obscura.core.smart_retry import with_smart_retry

        async def fail() -> Never:
            msg = "bad input"
            raise ValueError(msg)

        with pytest.raises(ValueError):
            await with_smart_retry(fail, max_retries=3, initial_backoff=0.01)

    async def test_exhausted_retries(self) -> None:
        from obscura.core.smart_retry import with_smart_retry

        async def always_fail() -> Never:
            msg = "503 Service Unavailable"
            raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="503"):
            await with_smart_retry(always_fail, max_retries=2, initial_backoff=0.01)


# ═══════════════════════════════════════════════════════════════════════════
# 7. Integration: KAIROS engine lifecycle
# ═══════════════════════════════════════════════════════════════════════════


class TestKairosIntegration:
    async def test_engine_start_stop(self) -> None:
        from obscura.kairos.engine import KairosEngine

        engine = KairosEngine()
        await engine.start()
        assert engine.is_running
        # start() itself logs "KAIROS engine started" = 1 observation.
        initial = engine.status()["observations"]
        engine.log("test event")
        assert engine.status()["observations"] == initial + 1
        await engine.stop()
        assert not engine.is_running

    def test_daily_log_persistence(self) -> None:
        from obscura.kairos.daily_log import DailyLog

        log = DailyLog()
        log.append("integration test entry", source="test")
        content = log.read()
        assert "integration test entry" in content

    def test_frustration_tracks_correctly(self) -> None:
        from obscura.kairos.frustration import FrustrationDetector

        d = FrustrationDetector()
        # Normal messages don't trigger.
        for _ in range(5):
            r = d.analyze("please fix the bug")
            assert not r.is_frustrated
        # Frustration triggers.
        r = d.analyze("wtf is wrong with this")
        assert r.is_frustrated
        assert r.consecutive_frustrations == 1
        r = d.analyze("this is total shit")
        assert r.consecutive_frustrations == 2
        # Positive resets.
        r = d.analyze("perfect, thanks")
        assert not r.is_frustrated
        assert r.sentiment == "positive"


# ═══════════════════════════════════════════════════════════════════════════
# 8. Integration: diff engine + render
# ═══════════════════════════════════════════════════════════════════════════


class TestDiffIntegration:
    def test_diff_engine_compute(self) -> None:
        from obscura.cli.app.diff_engine import DiffEngine

        engine = DiffEngine()
        hunks = engine.compute("hello\nworld\n", "hello\nplanet\n")
        assert len(hunks) > 0
        assert any(any(ln.tag == "+" for ln in h.lines) for h in hunks)

    def test_diff_side_by_side(self) -> None:
        from obscura.cli.app.diff_engine import DiffEngine

        engine = DiffEngine()
        fc = engine.compute_change(Path("test.py"), "old\n", "new\n")
        sbs = engine.format_side_by_side(fc)
        assert len(sbs) > 0

    def test_diff_apply_hunks(self) -> None:
        from obscura.cli.app.diff_engine import DiffEngine

        engine = DiffEngine()
        fc = engine.compute_change(Path("test.py"), "a\nb\nc\n", "a\nB\nc\n")
        for h in fc.hunks:
            h.accept()
        result = engine.apply_hunks("a\nb\nc\n", fc.hunks)
        assert "B" in result


# ═══════════════════════════════════════════════════════════════════════════
# 9. Integration: TUI effects don't crash
# ═══════════════════════════════════════════════════════════════════════════


class TestTUIEffects:
    def test_ultrathink_banner(self) -> None:
        from obscura.cli.tui_effects import ultrathink_banner

        # Should not crash.
        ultrathink_banner()

    def test_effort_badges(self) -> None:
        from obscura.cli.tui_effects import effort_badge

        for level in ["low", "medium", "high", "max"]:
            badge = effort_badge(level)
            assert len(badge) > 0

    def test_context_bar(self) -> None:
        from obscura.cli.tui_effects import context_bar

        for pct in [0.0, 0.3, 0.6, 0.85, 1.0]:
            bar = context_bar(pct)
            assert "%" in bar

    def test_gradient_text(self) -> None:
        from obscura.cli.tui_effects import gradient_text

        result = gradient_text("test")
        assert len(result) > len("test")  # Contains ANSI codes
