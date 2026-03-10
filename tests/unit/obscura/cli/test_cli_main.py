"""Full CLI integration tests for obscura.cli.

Covers: Click entry point, _repl(), send_message(), _cli_confirm(),
hooks loading, MCP discovery, file tracking, plan parsing.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from click.testing import CliRunner

import obscura.cli as cli_module
from obscura.cli import (
    main,
    send_message,
    _cli_confirm,
    _discover_mcp,
    _track_file_event,
    _parse_inline_agent_mention,
)
from obscura.cli.commands import REPLContext, _fleet_delegate, cmd_delegate
from obscura.core.types import AgentEvent, AgentEventKind


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(**overrides: Any) -> REPLContext:
    """Build a minimal REPLContext for testing."""
    defaults: dict[str, Any] = {
        "client": MagicMock(),
        "store": MagicMock(),
        "session_id": "test-session-123",
        "backend": "copilot",
        "model": "gpt-5-mini",
        "system_prompt": "You are a test agent.",
        "max_turns": 5,
        "tools_enabled": True,
        "confirm_enabled": False,
    }
    defaults.update(overrides)
    ctx = REPLContext(**defaults)
    # Give the mock client the properties send_message uses
    ctx.client.context_window = 128_000
    ctx.client.context_warn_threshold = 64_000
    return ctx


def _text_event(text: str) -> AgentEvent:
    return AgentEvent(kind=AgentEventKind.TEXT_DELTA, text=text)


def _tool_call_event(name: str, inp: dict[str, Any], use_id: str = "tc_1") -> AgentEvent:
    return AgentEvent(
        kind=AgentEventKind.TOOL_CALL,
        tool_name=name,
        tool_input=inp,
        tool_use_id=use_id,
    )


def _tool_result_event(result: str, use_id: str = "tc_1") -> AgentEvent:
    return AgentEvent(
        kind=AgentEventKind.TOOL_RESULT,
        tool_result=result,
        tool_use_id=use_id,
        is_error=False,
    )


def _done_event() -> AgentEvent:
    return AgentEvent(kind=AgentEventKind.AGENT_DONE)


# ---------------------------------------------------------------------------
# Click entry point
# ---------------------------------------------------------------------------


class TestClickEntryPoint:
    """Tests for the Click CLI command."""

    def test_help_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Obscura" in result.output
        assert "--backend" in result.output
        assert "--model" in result.output
        assert "--tools" in result.output

    def test_backend_choices(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        for name in ("copilot", "claude", "codex", "openai", "localllm", "moonshot"):
            assert name in result.output

    def test_invalid_backend_rejected(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["-b", "invalid_backend", "hello"])
        assert result.exit_code != 0

    def test_tools_choices(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert "on" in result.output
        assert "off" in result.output


# ---------------------------------------------------------------------------
# _cli_confirm
# ---------------------------------------------------------------------------


class TestCliConfirm:
    """Tests for the tool call confirmation callback."""

    @pytest.mark.asyncio
    async def test_always_list_skips_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _make_ctx(confirm_enabled=True)
        ctx.confirm_always.add("run_shell")

        called = False

        async def _fake_prompt(_msg: str) -> str:
            nonlocal called
            called = True
            return "n"

        monkeypatch.setattr(cli_module, "confirm_prompt_async", _fake_prompt)
        result = await _cli_confirm(ctx, "run_shell", {"cmd": "ls"})
        assert result is True
        assert not called

    @pytest.mark.asyncio
    async def test_user_says_yes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _make_ctx(confirm_enabled=True)

        from obscura.cli.widgets import WidgetResult

        async def _fake_confirm(_req: object) -> WidgetResult:
            return WidgetResult(action="allow")

        monkeypatch.setattr("obscura.cli.widgets.confirm_tool", _fake_confirm)
        result = await _cli_confirm(ctx, "write_file", {"path": "a.txt"})
        assert result is True

    @pytest.mark.asyncio
    async def test_user_says_no(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _make_ctx(confirm_enabled=True)

        from obscura.cli.widgets import WidgetResult

        async def _fake_confirm(_req: object) -> WidgetResult:
            return WidgetResult(action="deny")

        monkeypatch.setattr("obscura.cli.widgets.confirm_tool", _fake_confirm)
        result = await _cli_confirm(ctx, "write_file", {"path": "a.txt"})
        assert result is False

    @pytest.mark.asyncio
    async def test_user_says_always(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _make_ctx(confirm_enabled=True)

        from obscura.cli.widgets import WidgetResult

        async def _fake_confirm(_req: object) -> WidgetResult:
            return WidgetResult(action="always_allow")

        monkeypatch.setattr("obscura.cli.widgets.confirm_tool", _fake_confirm)
        result = await _cli_confirm(ctx, "edit_file", {"path": "b.py"})
        assert result is True
        assert "edit_file" in ctx.confirm_always


# ---------------------------------------------------------------------------
# _discover_mcp
# ---------------------------------------------------------------------------


class TestDiscoverMCP:
    """Tests for MCP server auto-discovery."""

    def test_returns_empty_when_no_servers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "obscura.integrations.mcp.config_loader.discover_mcp_servers",
            lambda: [],
        )
        configs, names = _discover_mcp()
        assert configs == []
        assert names == []

    def test_returns_empty_on_import_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # If the MCP module can't be imported, should return empty
        import obscura.cli as cli_mod

        original_discover = getattr(cli_mod, "_discover_mcp")
        # Force import error by patching
        with patch.dict("sys.modules", {"obscura.integrations.mcp.config_loader": None}):
            configs, names = _discover_mcp()
        assert configs == []
        assert names == []


# ---------------------------------------------------------------------------
# _track_file_event
# ---------------------------------------------------------------------------


class TestTrackFileEvent:
    """Tests for file change tracking."""

    def test_tracks_write_tool_calls(self, tmp_path: Path) -> None:
        ctx = _make_ctx()
        test_file = tmp_path / "test.txt"
        test_file.write_text("original content")

        event = _tool_call_event(
            "write_file",
            {"path": str(test_file)},
            use_id="tc_write_1",
        )
        _track_file_event(event.kind, ctx, event)

        assert "tc_write_1" in ctx._pending_file_reads
        path, before = ctx._pending_file_reads["tc_write_1"]
        assert path == str(test_file)
        assert before == "original content"

    def test_tracks_file_change_on_result(self, tmp_path: Path) -> None:
        ctx = _make_ctx()
        test_file = tmp_path / "test.txt"

        # Simulate the tool call phase
        ctx._pending_file_reads["tc_write_1"] = (str(test_file), "original")

        # Now write new content and simulate result
        test_file.write_text("modified")
        result_event = _tool_result_event("ok", use_id="tc_write_1")
        _track_file_event(result_event.kind, ctx, result_event)

        assert len(ctx._file_changes) == 1
        assert ctx._file_changes[0]["path"] == str(test_file)

    def test_no_change_recorded_when_content_unchanged(self, tmp_path: Path) -> None:
        ctx = _make_ctx()
        test_file = tmp_path / "test.txt"
        test_file.write_text("same content")

        ctx._pending_file_reads["tc_write_1"] = (str(test_file), "same content")
        result_event = _tool_result_event("ok", use_id="tc_write_1")
        _track_file_event(result_event.kind, ctx, result_event)

        assert len(ctx._file_changes) == 0

    def test_handles_nonexistent_file_for_new_creation(self, tmp_path: Path) -> None:
        ctx = _make_ctx()
        new_file = tmp_path / "new.txt"  # doesn't exist

        event = _tool_call_event(
            "write_file",
            {"path": str(new_file)},
            use_id="tc_create_1",
        )
        _track_file_event(event.kind, ctx, event)

        assert "tc_create_1" in ctx._pending_file_reads
        _, before = ctx._pending_file_reads["tc_create_1"]
        assert before == ""  # empty before for new file


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


class TestSendMessage:
    """Tests for the send_message function."""

    @pytest.mark.asyncio
    async def test_basic_text_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _make_ctx()

        # Mock run_loop to yield text events
        async def _fake_run_loop(*args: Any, **kwargs: Any):
            yield _text_event("Hello ")
            yield _text_event("World")
            yield _done_event()

        ctx.client.run_loop = _fake_run_loop

        # Suppress console output and other side effects
        monkeypatch.setattr(cli_module, "console", MagicMock())
        monkeypatch.setattr(cli_module, "trace_mod", MagicMock())
        monkeypatch.setattr(
            "obscura.tools.system.update_token_usage", lambda **kw: None
        )

        result = await send_message(ctx, "Hi there", {})
        assert result == "Hello World"

    @pytest.mark.asyncio
    async def test_message_history_updated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _make_ctx()

        async def _fake_run_loop(*args: Any, **kwargs: Any):
            yield _text_event("Response")

        ctx.client.run_loop = _fake_run_loop
        monkeypatch.setattr(cli_module, "console", MagicMock())
        monkeypatch.setattr(cli_module, "trace_mod", MagicMock())
        monkeypatch.setattr(
            "obscura.tools.system.update_token_usage", lambda **kw: None
        )

        await send_message(ctx, "Test prompt", {})

        assert len(ctx.message_history) == 2
        assert ctx.message_history[0] == ("user", "Test prompt")
        assert ctx.message_history[1] == ("assistant", "Response")

    @pytest.mark.asyncio
    async def test_empty_response_not_in_history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _make_ctx()

        async def _fake_run_loop(*args: Any, **kwargs: Any):
            yield _done_event()

        ctx.client.run_loop = _fake_run_loop
        monkeypatch.setattr(cli_module, "console", MagicMock())
        monkeypatch.setattr(cli_module, "trace_mod", MagicMock())
        monkeypatch.setattr(
            "obscura.tools.system.update_token_usage", lambda **kw: None
        )

        await send_message(ctx, "No response expected", {})

        assert len(ctx.message_history) == 1
        assert ctx.message_history[0] == ("user", "No response expected")

    @pytest.mark.asyncio
    async def test_keyboard_interrupt_handled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _make_ctx()

        async def _fake_run_loop(*args: Any, **kwargs: Any):
            yield _text_event("partial")
            raise KeyboardInterrupt

        ctx.client.run_loop = _fake_run_loop
        monkeypatch.setattr(cli_module, "console", MagicMock())
        monkeypatch.setattr(cli_module, "trace_mod", MagicMock())
        monkeypatch.setattr(
            "obscura.tools.system.update_token_usage", lambda **kw: None
        )

        # Should not raise
        result = await send_message(ctx, "Interrupted", {})
        assert result == "partial"

    @pytest.mark.asyncio
    async def test_auto_compact_on_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _make_ctx()
        ctx.client.context_window = 1000  # small window
        ctx.client.context_warn_threshold = 500

        # Simulate high token count that exceeds 60% threshold (600)
        token_count = 700

        async def _fake_run_loop(*args: Any, **kwargs: Any):
            yield _text_event("ok")

        ctx.client.run_loop = _fake_run_loop
        mock_console = MagicMock()
        monkeypatch.setattr(cli_module, "console", mock_console)
        monkeypatch.setattr(cli_module, "trace_mod", MagicMock())
        monkeypatch.setattr(
            "obscura.tools.system.update_token_usage", lambda **kw: None
        )
        monkeypatch.setattr(
            "obscura.cli.commands.estimate_effective_context_tokens",
            lambda *_a, **_kw: token_count,
        )

        compact_called = False
        original_cmd_compact = None

        async def _fake_compact(level: str, ctx: Any) -> None:
            nonlocal compact_called
            compact_called = True

        monkeypatch.setattr("obscura.cli.commands.cmd_compact", _fake_compact)

        await send_message(ctx, "test", {})
        assert compact_called

    @pytest.mark.asyncio
    async def test_vector_memory_augmentation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _make_ctx()

        # Mock vector store
        mock_vs = MagicMock()
        ctx.vector_store = mock_vs

        captured_text: list[str] = []

        async def _fake_run_loop(text: str, **kwargs: Any):
            captured_text.append(text)
            yield _text_event("ok")

        ctx.client.run_loop = _fake_run_loop
        monkeypatch.setattr(cli_module, "console", MagicMock())
        monkeypatch.setattr(cli_module, "trace_mod", MagicMock())
        monkeypatch.setattr(
            "obscura.tools.system.update_token_usage", lambda **kw: None
        )
        monkeypatch.setattr(
            cli_module,
            "search_relevant_context",
            lambda vs, q, top_k: "[memory] relevant info",
        )
        monkeypatch.setattr(
            cli_module,
            "auto_save_turn",
            lambda *a, **kw: None,
        )

        await send_message(ctx, "my question", {})

        assert len(captured_text) == 1
        assert "[memory] relevant info" in captured_text[0]
        assert "my question" in captured_text[0]

    @pytest.mark.asyncio
    async def test_active_skill_context_augmentation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _make_ctx()

        captured_text: list[str] = []

        async def _fake_run_loop(text: str, **kwargs: Any):
            captured_text.append(text)
            yield _text_event("ok")

        ctx.client.run_loop = _fake_run_loop
        monkeypatch.setattr(cli_module, "console", MagicMock())
        monkeypatch.setattr(cli_module, "trace_mod", MagicMock())
        monkeypatch.setattr(
            "obscura.tools.system.update_token_usage", lambda **kw: None
        )
        monkeypatch.setattr(
            cli_module,
            "search_relevant_context",
            lambda vs, q, top_k: "",
        )
        monkeypatch.setattr(
            ctx,
            "build_active_skill_context",
            lambda: "## Active Slash Skills\n\n## Skill: reviewer\n\nFocus on regressions.",
        )

        await send_message(ctx, "check this diff", {})

        assert len(captured_text) == 1
        assert "Active Slash Skills" in captured_text[0]
        assert "check this diff" in captured_text[0]

    @pytest.mark.asyncio
    async def test_dead_process_error_retries_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import obscura.cli as cli_module

        ctx = _make_ctx()
        ctx.client.reset_session = AsyncMock()
        calls = 0

        async def _fake_run_loop(*args: Any, **kwargs: Any):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("can't send message to dead process")
            yield _text_event("Recovered")

        ctx.client.run_loop = _fake_run_loop
        monkeypatch.setattr(cli_module, "console", MagicMock())
        monkeypatch.setattr(cli_module, "trace_mod", MagicMock())
        monkeypatch.setattr("obscura.cli.commands._estimate_tokens", lambda x: 100)
        monkeypatch.setattr("obscura.tools.system.update_token_usage", lambda **kw: None)

        result = await send_message(ctx, "retry me", {})

        assert result == "Recovered"
        assert calls == 2
        ctx.client.reset_session.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_context_usage_updates_during_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import obscura.cli as cli_module

        ctx = _make_ctx()
        chunk = "x" * 140  # 35 token estimate per chunk

        async def _fake_run_loop(*args: Any, **kwargs: Any):
            yield _text_event(chunk)
            yield _text_event(chunk)
            yield _text_event(chunk)
            yield _done_event()

        ctx.client.run_loop = _fake_run_loop

        calls: list[dict[str, Any]] = []

        def _capture_usage(**kw: Any) -> None:
            calls.append(kw)

        ticks = iter([1.0, 2.0, 3.0, 4.0, 5.0])
        last_tick = 5.0

        def _fake_monotonic() -> float:
            nonlocal last_tick
            try:
                last_tick = next(ticks)
            except StopIteration:
                pass
            return last_tick

        monkeypatch.setattr(cli_module, "console", MagicMock())
        monkeypatch.setattr(cli_module, "trace_mod", MagicMock())
        monkeypatch.setattr("obscura.cli.commands._estimate_tokens", lambda x: 100)
        monkeypatch.setattr("obscura.tools.system.update_token_usage", _capture_usage)
        monkeypatch.setattr(cli_module.time, "monotonic", _fake_monotonic)

        await send_message(ctx, "stream please", {})

        # pre + streamed increments + force flush + post
        assert len(calls) >= 5
        # At least one mid-stream update should include non-zero output tokens.
        assert any(int(c.get("output_tokens", 0)) > 0 for c in calls)

    @pytest.mark.asyncio
    async def test_inline_agent_mention_short_circuits_base_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import obscura.cli as cli_module

        ctx = _make_ctx()
        ctx.client.run_loop = AsyncMock()
        monkeypatch.setattr(
            cli_module,
            "_run_inline_agent_from_mention",
            AsyncMock(return_value="inline agent output"),
        )

        result = await send_message(ctx, "@researcher find relevant files", {})

        assert result == "inline agent output"
        assert ctx.message_history[-2] == (
            "user",
            "@researcher find relevant files",
        )
        assert ctx.message_history[-1] == ("assistant", "inline agent output")
        assert ctx.client.run_loop.call_count == 0


class TestInlineAgentMentionParsing:
    def test_parse_valid_mention(self) -> None:
        assert _parse_inline_agent_mention("@researcher summarize this") == (
            "researcher",
            "summarize this",
        )

    def test_parse_invalid_without_prompt(self) -> None:
        assert _parse_inline_agent_mention("@researcher") is None


# ---------------------------------------------------------------------------
# Hooks loading in CLI
# ---------------------------------------------------------------------------


class TestHooksLoading:
    """Tests for project hooks loading wired into CLI."""

    def test_load_all_hooks_empty_when_no_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """load_all_hooks returns empty registry when no .obscura dir exists."""
        from obscura.core.settings import load_all_hooks

        monkeypatch.setattr(
            "obscura.core.paths.resolve_obscura_home", lambda cwd=None: tmp_path
        )
        registry = load_all_hooks()
        assert registry.count == 0

    def test_load_settings_hooks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Hooks from settings.json are loaded correctly."""
        from obscura.core.settings import load_settings_hooks

        settings = {
            "hooks": {
                "preToolUse": [
                    {"bash": "echo check", "matcher": "run_shell", "timeout_sec": 5}
                ],
                "postToolUse": [
                    {"bash": "echo done"}
                ],
            }
        }
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps(settings))

        monkeypatch.setattr(
            "obscura.core.settings.resolve_obscura_settings", lambda cwd=None: settings_file
        )
        defs = load_settings_hooks()
        assert len(defs) == 2

        pre_hook = defs[0]
        assert pre_hook.event == "preToolUse"
        assert pre_hook.bash == "echo check"
        assert pre_hook.matcher == "run_shell"
        assert pre_hook.timeout_sec == 5

        post_hook = defs[1]
        assert post_hook.event == "postToolUse"
        assert post_hook.bash == "echo done"
        assert post_hook.matcher == ""

    def test_load_settings_hooks_malformed_json(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Malformed settings.json returns empty list."""
        from obscura.core.settings import load_settings_hooks

        settings_file = tmp_path / "settings.json"
        settings_file.write_text("not valid json {{{")

        monkeypatch.setattr(
            "obscura.core.settings.resolve_obscura_settings", lambda cwd=None: settings_file
        )
        defs = load_settings_hooks()
        assert defs == []

    def test_load_directory_hooks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Hooks from .obscura/hooks/ directory are loaded correctly."""
        from obscura.core.settings import load_directory_hooks

        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()

        # Create executable hook scripts
        script1 = hooks_dir / "pre-tool-use.sh"
        script1.write_text("#!/bin/bash\necho ok")
        script1.chmod(0o755)

        script2 = hooks_dir / "post-tool-use--run_shell.py"
        script2.write_text("#!/usr/bin/env python3\nprint('ok')")
        script2.chmod(0o755)

        script3 = hooks_dir / "session-init.sh"
        script3.write_text("#!/bin/bash\necho session")
        script3.chmod(0o755)

        monkeypatch.setattr(
            "obscura.core.settings.resolve_obscura_hooks_dir", lambda cwd=None: hooks_dir
        )
        defs = load_directory_hooks()
        assert len(defs) == 3

        # Sort by event for deterministic checking
        defs.sort(key=lambda d: d.event)

        post_hook = defs[0]
        assert post_hook.event == "postToolUse"
        assert post_hook.matcher == "run_shell"
        assert "python3" in post_hook.bash

        pre_hook = defs[1]
        assert pre_hook.event == "preToolUse"
        assert pre_hook.matcher == ""
        assert "bash" in pre_hook.bash

        session_hook = defs[2]
        assert session_hook.event == "sessionStart"
        assert session_hook.matcher == ""
        assert "bash" in session_hook.bash

    def test_non_executable_scripts_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-executable scripts in hooks/ are skipped with a warning."""
        from obscura.core.settings import load_directory_hooks

        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()

        script = hooks_dir / "pre-tool-use.sh"
        script.write_text("#!/bin/bash\necho ok")
        script.chmod(0o644)  # not executable

        monkeypatch.setattr(
            "obscura.core.settings.resolve_obscura_hooks_dir", lambda cwd=None: hooks_dir
        )
        defs = load_directory_hooks()
        assert len(defs) == 0

    def test_list_hook_sources(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """list_hook_sources returns metadata for all hooks."""
        from obscura.core.settings import list_hook_sources

        settings = {
            "hooks": {
                "preToolUse": [{"bash": "lint.sh", "matcher": "run_shell"}],
            }
        }
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps(settings))

        monkeypatch.setattr(
            "obscura.core.settings.resolve_obscura_settings", lambda cwd=None: settings_file
        )
        monkeypatch.setattr(
            "obscura.core.settings.resolve_obscura_hooks_dir", lambda cwd=None: tmp_path / "nope"
        )

        sources = list_hook_sources()
        assert len(sources) == 1
        assert sources[0]["source"] == "settings.json"
        assert sources[0]["event"] == "preToolUse"
        assert sources[0]["matcher"] == "run_shell"


# ---------------------------------------------------------------------------
# HookDefinition matcher filtering
# ---------------------------------------------------------------------------


class TestMatcherFiltering:
    """Tests for the matcher field on HookDefinition."""

    @pytest.mark.asyncio
    async def test_before_hook_skips_non_matching_tool(self) -> None:
        """Before-hook with matcher should pass through when tool doesn't match."""
        from obscura.core.hooks import _make_command_before_hook
        from obscura.manifest.models import HookDefinition

        defn = HookDefinition(
            event="preToolUse",
            bash="echo should_not_run",
            matcher="run_shell",
        )
        hook = _make_command_before_hook(defn)

        event = AgentEvent(
            kind=AgentEventKind.TOOL_CALL,
            tool_name="write_file",
            tool_input={"path": "a.txt"},
        )
        result = await hook(event)
        assert result is event  # passed through unmodified

    @pytest.mark.asyncio
    async def test_before_hook_runs_for_matching_tool(self) -> None:
        """Before-hook with matcher should run when tool matches."""
        from obscura.core.hooks import _make_command_before_hook
        from obscura.manifest.models import HookDefinition

        defn = HookDefinition(
            event="preToolUse",
            bash="echo '{}'",  # outputs valid JSON (empty object)
            matcher="run_shell",
        )
        hook = _make_command_before_hook(defn)

        event = AgentEvent(
            kind=AgentEventKind.TOOL_CALL,
            tool_name="run_shell",
            tool_input={"cmd": "ls"},
        )
        result = await hook(event)
        assert result is event  # didn't deny

    @pytest.mark.asyncio
    async def test_before_hook_wildcard_always_runs(self) -> None:
        """Before-hook with empty matcher should run for any tool."""
        from obscura.core.hooks import _make_command_before_hook
        from obscura.manifest.models import HookDefinition

        defn = HookDefinition(
            event="preToolUse",
            bash="echo '{}'",
            matcher="",  # wildcard
        )
        hook = _make_command_before_hook(defn)

        event = AgentEvent(
            kind=AgentEventKind.TOOL_CALL,
            tool_name="any_tool",
            tool_input={},
        )
        result = await hook(event)
        assert result is event

    @pytest.mark.asyncio
    async def test_after_hook_skips_non_matching_tool(self) -> None:
        """After-hook with matcher should skip when tool doesn't match."""
        from obscura.core.hooks import _make_command_after_hook
        from obscura.manifest.models import HookDefinition

        ran = {"called": False}

        defn = HookDefinition(
            event="postToolUse",
            bash="echo should_not_run",
            matcher="run_shell",
        )
        hook = _make_command_after_hook(defn)

        event = AgentEvent(
            kind=AgentEventKind.TOOL_RESULT,
            tool_name="write_file",
            tool_result="ok",
        )
        # Should complete without running the bash command
        await hook(event)


# ---------------------------------------------------------------------------
# Tool policy — apply_to_copilot
# ---------------------------------------------------------------------------


class TestApplyToCopilot:
    """Tests that apply_to_copilot keeps ToolSpec objects (not dicts)."""

    def test_keeps_toolspec_objects(self) -> None:
        """config['tools'] should contain ToolSpec objects, not dicts."""
        from obscura.core.tool_policy import ToolPolicy
        from obscura.core.types import ToolSpec

        async def noop(**kw: Any) -> str:
            return ""

        tools = [
            ToolSpec(name="search", description="Search", parameters={}, handler=noop),
            ToolSpec(name="fetch", description="Fetch", parameters={}, handler=noop),
        ]

        policy = ToolPolicy.custom_only()
        config: dict[str, Any] = {}
        policy.apply_to_copilot(config, tools)

        assert "tools" in config
        # Should be ToolSpec objects, not dicts
        for t in config["tools"]:
            assert hasattr(t, "name"), f"Expected ToolSpec with .name, got {type(t)}"

    def test_filters_with_allowed_tools(self) -> None:
        """Only allowed tools should appear in config."""
        from obscura.core.tool_policy import ToolPolicy
        from obscura.core.types import ToolSpec

        async def noop(**kw: Any) -> str:
            return ""

        tools = [
            ToolSpec(name="search", description="Search", parameters={}, handler=noop),
            ToolSpec(name="fetch", description="Fetch", parameters={}, handler=noop),
            ToolSpec(name="delete", description="Delete", parameters={}, handler=noop),
        ]

        policy = ToolPolicy.restricted(["search", "fetch"])
        config: dict[str, Any] = {}
        policy.apply_to_copilot(config, tools)

        names = [t.name for t in config["tools"]]
        assert "search" in names
        assert "fetch" in names
        assert "delete" not in names

    def test_sets_allowed_tools_when_native_blocked(self) -> None:
        """allowed_tools list should be set when allow_native=False."""
        from obscura.core.tool_policy import ToolPolicy
        from obscura.core.types import ToolSpec

        async def noop(**kw: Any) -> str:
            return ""

        tools = [
            ToolSpec(name="search", description="Search", parameters={}, handler=noop),
        ]

        policy = ToolPolicy.custom_only()
        config: dict[str, Any] = {}
        policy.apply_to_copilot(config, tools)

        assert config["allowed_tools"] == ["search"]

    def test_no_allowed_tools_when_native_allowed(self) -> None:
        """allowed_tools should not be set when allow_native=True."""
        from obscura.core.tool_policy import ToolPolicy
        from obscura.core.types import ToolSpec

        async def noop(**kw: Any) -> str:
            return ""

        tools = [
            ToolSpec(name="search", description="Search", parameters={}, handler=noop),
        ]

        policy = ToolPolicy.allow_all()
        config: dict[str, Any] = {}
        policy.apply_to_copilot(config, tools)

        assert "allowed_tools" not in config

    def test_empty_tools_noop(self) -> None:
        """Empty tools list should not modify config."""
        from obscura.core.tool_policy import ToolPolicy

        policy = ToolPolicy.custom_only()
        config: dict[str, Any] = {}
        policy.apply_to_copilot(config, [])

        assert "tools" not in config


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


class TestPathHelpers:
    """Tests for the new path resolution functions."""

    def test_resolve_hooks_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from obscura.core.paths import resolve_obscura_hooks_dir

        monkeypatch.setattr(
            "obscura.core.paths.resolve_obscura_home", lambda cwd=None: tmp_path
        )
        assert resolve_obscura_hooks_dir() == tmp_path / "hooks"

    def test_resolve_settings(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from obscura.core.paths import resolve_obscura_settings

        monkeypatch.setattr(
            "obscura.core.paths.resolve_obscura_home", lambda cwd=None: tmp_path
        )
        assert resolve_obscura_settings() == tmp_path / "settings.json"


# ---------------------------------------------------------------------------
# REPLContext
# ---------------------------------------------------------------------------


class TestREPLContext:
    """Tests for REPLContext initialization and behavior."""

    def test_default_state(self) -> None:
        ctx = _make_ctx()
        assert ctx.session_id == "test-session-123"
        assert ctx.backend == "copilot"
        assert ctx.tools_enabled is True
        assert ctx.confirm_enabled is False
        assert len(ctx.confirm_always) == 0
        assert len(ctx.message_history) == 0
        assert len(ctx._file_changes) == 0

    def test_add_file_change(self) -> None:
        ctx = _make_ctx()
        ctx.add_file_change("/path/to/file.py", "before", "after")
        assert len(ctx._file_changes) == 1
        change = ctx._file_changes[0]
        assert change["path"] == "/path/to/file.py"

    def test_mode_manager_lazy_creation(self) -> None:
        ctx = _make_ctx()
        assert ctx._mode_manager is None
        mm = ctx.get_mode_manager()
        assert mm is not None
        # Second call returns same instance
        assert ctx.get_mode_manager() is mm


# ---------------------------------------------------------------------------
# Delegate modes
# ---------------------------------------------------------------------------


class TestDelegateModes:
    @pytest.mark.asyncio
    async def test_cmd_delegate_once_mode_calls_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _make_ctx()

        class _Cfg:
            name = "delegate"

        class _FakeAgent:
            config = _Cfg()

            async def start(self) -> None:
                return None

            async def stop(self) -> None:
                return None

            async def run(self, prompt: str) -> str:
                assert prompt == "write tests"
                return "done"

        runtime = MagicMock()
        runtime.spawn.return_value = _FakeAgent()
        ctx.get_runtime = AsyncMock(return_value=runtime)  # type: ignore[method-assign]

        monkeypatch.setattr("obscura.cli.commands.console", MagicMock())
        monkeypatch.setattr("obscura.cli.commands.print_info", MagicMock())
        monkeypatch.setattr("obscura.cli.commands.print_ok", MagicMock())

        await cmd_delegate("--mode once --model claude write tests", ctx)
        runtime.spawn.assert_called_once()

    @pytest.mark.asyncio
    async def test_fleet_delegate_loop_with_done_if_continues_until_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _make_ctx()
        events_by_call = [
            [
                AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="partial"),
                AgentEvent(kind=AgentEventKind.AGENT_DONE),
            ],
            [
                AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="all done marker"),
                AgentEvent(kind=AgentEventKind.AGENT_DONE),
            ],
        ]
        calls = {"count": 0}

        class _Cfg:
            name = "worker-1"

        class _FakeAgent:
            config = _Cfg()

            async def run(self, prompt: str) -> str:
                return prompt

            async def stream_loop(self, prompt: str, **kwargs: Any):
                idx = calls["count"]
                calls["count"] += 1
                for ev in events_by_call[idx]:
                    yield ev

        agent = _FakeAgent()
        runtime = MagicMock()
        runtime.get_agent.return_value = agent
        runtime.list_agents.return_value = [agent]
        ctx.get_runtime = AsyncMock(return_value=runtime)  # type: ignore[method-assign]

        renderer = MagicMock()
        monkeypatch.setattr(
            "obscura.cli.render.LabeledStreamRenderer",
            lambda *_a, **_kw: renderer,
        )
        monkeypatch.setattr("obscura.cli.commands.console", MagicMock())
        monkeypatch.setattr("obscura.cli.commands.print_ok", MagicMock())

        await _fleet_delegate(
            'worker-1 --mode loop --passes 2 --done-if "done marker" finish task',
            ctx,
        )
        assert calls["count"] == 2
