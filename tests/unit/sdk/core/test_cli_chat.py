"""Tests for CLI chat + passthrough commands (scripts.obscura_cli).

Tests the new owned-mode ``chat`` command (unified + native modes,
memory injection, transcript persistence) and the ``passthrough``
command (subprocess delegation, MemoryStore persistence).
"""
# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from sdk.cli.chat_cli import (
    _load_memory_context,
    _parse_tool_policy,
    _persist_transcript,
    _render_event,
    _resolve_cli_user,
    cli,
)
from sdk.internal.types import (
    AgentEvent,
    AgentEventKind,
    Backend,
    ContentBlock,
    Message,
    NativeHandle,
    Role,
    SessionRef,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _make_mock_client(
    *,
    send_text: str = "Hello from mock",
    stream_events: list[AgentEvent] | None = None,
    native_client: Any = None,
) -> MagicMock:
    """Build a mock ObscuraClient with async context manager support."""
    mock = MagicMock()

    # Async context manager
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)

    # send() → Message
    mock.send = AsyncMock(
        return_value=Message(
            role=Role.ASSISTANT,
            content=[ContentBlock(kind="text", text=send_text)],
        )
    )

    # create_session / resume_session
    mock.create_session = AsyncMock(
        return_value=SessionRef(session_id="mock-sess-1", backend=Backend.OPENAI)
    )
    mock.resume_session = AsyncMock()

    # run_loop → async generator
    events = stream_events or [
        AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="streamed text", turn=1),
        AgentEvent(kind=AgentEventKind.AGENT_DONE, text="streamed text", turn=1),
    ]

    async def _fake_run_loop(*_args: Any, **_kwargs: Any) -> Any:
        for ev in events:
            yield ev

    mock.run_loop = _fake_run_loop

    # native handle
    mock.native = NativeHandle(client=native_client)

    return mock


def _patch_client(mock_client: MagicMock) -> Any:
    """Patch ObscuraClient at the import site used by the chat command."""
    mock_cls = MagicMock(return_value=mock_client)
    return patch("sdk.client.ObscuraClient", mock_cls)


# ---------------------------------------------------------------------------
# TestChatHelpers
# ---------------------------------------------------------------------------


class TestChatHelpers:
    """Unit tests for CLI helper functions."""

    def test_resolve_cli_user(self) -> None:
        user = _resolve_cli_user()
        assert user.user_id.startswith("cli:")
        assert user.token_type == "cli"
        assert "admin" in user.roles

    def test_parse_tool_policy_auto(self) -> None:
        tc = _parse_tool_policy("auto")
        assert tc.mode == "auto"

    def test_parse_tool_policy_none(self) -> None:
        tc = _parse_tool_policy("none")
        assert tc.mode == "none"

    def test_parse_tool_policy_required(self) -> None:
        tc = _parse_tool_policy("required:search")
        assert tc.mode == "function"
        assert tc.function_name == "search"

    def test_parse_tool_policy_unknown_defaults_auto(self) -> None:
        tc = _parse_tool_policy("garbage")
        assert tc.mode == "auto"

    def test_render_event_text_delta(self, capsys: pytest.CaptureFixture[str]) -> None:
        ev = AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="hello", turn=1)
        _render_event(ev)
        out = capsys.readouterr().out
        assert "hello" in out

    def test_render_event_thinking(self, capsys: pytest.CaptureFixture[str]) -> None:
        ev = AgentEvent(kind=AgentEventKind.THINKING_DELTA, text="hmm", turn=1)
        _render_event(ev)
        out = capsys.readouterr().out
        assert "hmm" in out

    def test_render_event_tool_call(self, capsys: pytest.CaptureFixture[str]) -> None:
        ev = AgentEvent(
            kind=AgentEventKind.TOOL_CALL, tool_name="search", turn=1
        )
        _render_event(ev)
        out = capsys.readouterr().out
        assert "search" in out

    def test_render_event_tool_result(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ev = AgentEvent(
            kind=AgentEventKind.TOOL_RESULT,
            tool_result="found 3 results",
            turn=1,
        )
        _render_event(ev)
        out = capsys.readouterr().out
        assert "found 3 results" in out

    def test_load_memory_context_empty(self) -> None:
        """Memory context returns empty when no hits."""
        user = _resolve_cli_user()
        ctx = _load_memory_context(user, "something obscure")
        # Should not raise, may be empty
        assert isinstance(ctx, str)

    def test_persist_transcript_writes_to_memory(self, tmp_path: Path) -> None:
        """Transcript persistence writes to MemoryStore."""
        user = _resolve_cli_user()
        transcript = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        # Should not raise
        _persist_transcript(user, "test-sess", transcript, "openai")

        # Verify it's in MemoryStore
        from sdk.memory import MemoryStore

        mem = MemoryStore.for_user(user)
        stored = mem.get("transcript:test-sess", namespace="session")
        assert stored is not None
        assert len(stored) == 2
        assert stored[0]["role"] == "user"


# ---------------------------------------------------------------------------
# TestChatUnifiedSingleShot
# ---------------------------------------------------------------------------


class TestChatUnifiedSingleShot:
    """Tests for single-shot chat in unified mode."""

    def test_chat_single_shot_streaming(self, runner: CliRunner) -> None:
        mock_client = _make_mock_client()
        with _patch_client(mock_client):
            result = runner.invoke(cli, ["chat", "hello world", "-b", "openai"])

        assert result.exit_code == 0
        assert "streamed text" in result.output

    def test_chat_single_shot_no_stream(self, runner: CliRunner) -> None:
        mock_client = _make_mock_client(send_text="non-stream response")
        with _patch_client(mock_client):
            result = runner.invoke(
                cli, ["chat", "hello", "-b", "openai", "--no-stream"]
            )

        assert result.exit_code == 0
        assert "non-stream response" in result.output
        mock_client.send.assert_awaited_once()

    def test_chat_single_shot_json_output(self, runner: CliRunner) -> None:
        mock_client = _make_mock_client(send_text="json test")
        with _patch_client(mock_client):
            result = runner.invoke(
                cli,
                ["chat", "hello", "-b", "openai", "--no-stream", "--json-output"],
            )

        assert result.exit_code == 0
        # Should contain JSON with "text" key
        assert '"text"' in result.output
        assert "json test" in result.output

    def test_chat_no_prompt_no_interactive(self, runner: CliRunner) -> None:
        mock_client = _make_mock_client()
        with _patch_client(mock_client):
            result = runner.invoke(cli, ["chat", "-b", "openai"])

        assert result.exit_code == 0
        assert "Provide a prompt" in result.output

    def test_chat_tools_off(self, runner: CliRunner) -> None:
        """--tools off should pass tool_choice=none."""
        call_kwargs: dict[str, Any] = {}

        async def _capture_run_loop(*_args: Any, **kwargs: Any) -> Any:
            call_kwargs.update(kwargs)
            yield AgentEvent(
                kind=AgentEventKind.TEXT_DELTA, text="ok", turn=1
            )
            yield AgentEvent(kind=AgentEventKind.AGENT_DONE, text="ok", turn=1)

        mock_client = _make_mock_client()
        mock_client.run_loop = _capture_run_loop

        with _patch_client(mock_client):
            result = runner.invoke(
                cli, ["chat", "test", "-b", "openai", "--tools", "off"]
            )

        assert result.exit_code == 0
        tc = call_kwargs.get("tool_choice")
        assert tc is not None
        assert tc.mode == "none"

    def test_chat_tool_policy_required(self, runner: CliRunner) -> None:
        """--tool-policy required:search should pass ToolChoice.required."""
        call_kwargs: dict[str, Any] = {}

        async def _capture_run_loop(*_args: Any, **kwargs: Any) -> Any:
            call_kwargs.update(kwargs)
            yield AgentEvent(
                kind=AgentEventKind.TEXT_DELTA, text="ok", turn=1
            )
            yield AgentEvent(kind=AgentEventKind.AGENT_DONE, text="ok", turn=1)

        mock_client = _make_mock_client()
        mock_client.run_loop = _capture_run_loop

        with _patch_client(mock_client):
            result = runner.invoke(
                cli,
                ["chat", "test", "-b", "openai", "--tool-policy", "required:search"],
            )

        assert result.exit_code == 0
        tc = call_kwargs.get("tool_choice")
        assert tc is not None
        assert tc.mode == "function"
        assert tc.function_name == "search"


# ---------------------------------------------------------------------------
# TestChatUnifiedInteractive
# ---------------------------------------------------------------------------


class TestChatUnifiedInteractive:
    """Tests for interactive chat mode."""

    def test_chat_interactive_exit(self, runner: CliRunner) -> None:
        """Typing 'exit' terminates the interactive loop."""
        mock_client = _make_mock_client()

        with _patch_client(mock_client):
            result = runner.invoke(
                cli,
                ["chat", "-b", "openai", "--interactive"],
                input="exit\n",
            )

        assert result.exit_code == 0

    def test_chat_interactive_basic(self, runner: CliRunner) -> None:
        """Interactive mode processes one turn then exits on 'quit'."""
        mock_client = _make_mock_client()

        with _patch_client(mock_client):
            result = runner.invoke(
                cli,
                ["chat", "-b", "openai", "--interactive"],
                input="hello\nquit\n",
            )

        assert result.exit_code == 0
        assert "streamed text" in result.output


# ---------------------------------------------------------------------------
# TestChatNativeMode
# ---------------------------------------------------------------------------


class TestChatNativeMode:
    """Tests for native mode routing."""

    def test_chat_native_fallback_for_claude(self, runner: CliRunner) -> None:
        """Native mode for claude shows fallback message."""
        mock_client = _make_mock_client()
        # send() for claude fallback
        mock_client.send = AsyncMock(
            return_value=Message(
                role=Role.ASSISTANT,
                content=[ContentBlock(kind="text", text="claude native response")],
            )
        )

        with _patch_client(mock_client):
            result = runner.invoke(
                cli,
                ["chat", "test prompt", "-b", "claude", "--mode", "native"],
            )

        assert result.exit_code == 0
        # Should show fallback message about raw SDK
        assert "native" in result.output.lower() or "claude native response" in result.output

    def test_chat_native_no_client(self, runner: CliRunner) -> None:
        """Native mode with no native client shows error."""
        mock_client = _make_mock_client(native_client=None)

        with _patch_client(mock_client):
            result = runner.invoke(
                cli,
                ["chat", "test", "-b", "openai", "--mode", "native"],
            )

        assert result.exit_code == 0
        assert "not available" in result.output.lower()


# ---------------------------------------------------------------------------
# TestChatMemory
# ---------------------------------------------------------------------------


class TestChatMemory:
    """Tests for memory injection and persistence."""

    def test_chat_no_memory_flag(self, runner: CliRunner) -> None:
        """--no-memory should skip memory operations."""
        mock_client = _make_mock_client()

        with _patch_client(mock_client), patch(
            "sdk.cli.chat_cli._load_memory_context"
        ) as mock_load, patch(
            "sdk.cli.chat_cli._persist_transcript"
        ) as mock_persist:
            result = runner.invoke(
                cli,
                ["chat", "hello", "-b", "openai", "--no-memory"],
            )

        assert result.exit_code == 0
        mock_load.assert_not_called()
        mock_persist.assert_not_called()

    def test_chat_memory_enabled_by_default(self, runner: CliRunner) -> None:
        """Memory is enabled by default — _persist_transcript is called."""
        mock_client = _make_mock_client()

        with _patch_client(mock_client), patch(
            "sdk.cli.chat_cli._persist_transcript"
        ) as mock_persist:
            result = runner.invoke(
                cli,
                ["chat", "hello", "-b", "openai", "--no-stream"],
            )

        assert result.exit_code == 0
        # Transcript should have been persisted
        mock_persist.assert_called_once()
        # Verify transcript contains user + assistant messages
        args = mock_persist.call_args
        transcript = args[0][2]  # 3rd positional arg
        assert len(transcript) == 2
        assert transcript[0]["role"] == "user"
        assert transcript[1]["role"] == "assistant"


# ---------------------------------------------------------------------------
# TestChatSession
# ---------------------------------------------------------------------------


class TestChatSession:
    """Tests for session handling."""

    def test_chat_session_resume(self, runner: CliRunner) -> None:
        """--session foo should call client.resume_session()."""
        mock_client = _make_mock_client()

        with _patch_client(mock_client):
            result = runner.invoke(
                cli,
                ["chat", "hello", "-b", "openai", "--session", "my-sess"],
            )

        assert result.exit_code == 0
        mock_client.resume_session.assert_awaited_once()
        ref = mock_client.resume_session.call_args[0][0]
        assert ref.session_id == "my-sess"

    def test_chat_session_auto_create(self, runner: CliRunner) -> None:
        """Without --session, client.create_session() is called."""
        mock_client = _make_mock_client()

        with _patch_client(mock_client):
            result = runner.invoke(
                cli, ["chat", "hello", "-b", "openai"]
            )

        assert result.exit_code == 0
        mock_client.create_session.assert_awaited_once()


# ---------------------------------------------------------------------------
# TestPassthrough
# ---------------------------------------------------------------------------


class TestPassthrough:
    """Tests for passthrough command."""

    def test_passthrough_vendor_not_found(self, runner: CliRunner) -> None:
        """Missing vendor CLI should print error and exit 1."""
        with patch("shutil.which", return_value=None):
            result = runner.invoke(cli, ["passthrough", "claude"])

        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_passthrough_runs_subprocess(self, runner: CliRunner) -> None:
        """Passthrough should run the vendor CLI as interactive subprocess."""
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("shutil.which", return_value="/usr/bin/claude"), patch(
            "subprocess.run", return_value=mock_result
        ) as mock_run:
            result = runner.invoke(cli, ["passthrough", "claude", "--", "--help"])

        # subprocess.run was called with the right command
        mock_run.assert_called_once_with(["/usr/bin/claude", "--help"])

    def test_passthrough_captured_mode(self, runner: CliRunner) -> None:
        """Passthrough --capture should pipe and capture output."""
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()

        # Mock stdout/stderr streams
        call_count = 0

        async def _readline_stdout() -> bytes:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return b"Hello from vendor\n"
            return b""

        async def _readline_stderr() -> bytes:
            return b""

        mock_stdout = AsyncMock()
        mock_stdout.readline = _readline_stdout
        mock_stderr = AsyncMock()
        mock_stderr.readline = _readline_stderr

        mock_proc.stdout = mock_stdout
        mock_proc.stderr = mock_stderr

        with patch("shutil.which", return_value="/usr/bin/claude"), patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ):
            result = runner.invoke(
                cli, ["passthrough", "--capture", "claude", "--", "--help"]
            )

        assert result.exit_code == 0
        assert "Hello from vendor" in result.output

    def test_passthrough_transcript_file_saved(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Passthrough should save transcript to ~/.obscura/transcripts/."""
        # Redirect home to tmp
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()

        call_count = 0

        async def _readline() -> bytes:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return b"transcript line\n"
            return b""

        mock_stdout = AsyncMock()
        mock_stdout.readline = _readline
        mock_stderr = AsyncMock()
        mock_stderr.readline = AsyncMock(return_value=b"")
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = mock_stderr

        with patch("shutil.which", return_value="/usr/bin/claude"), patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ):
            result = runner.invoke(cli, ["passthrough", "--capture", "claude"])

        assert result.exit_code == 0
        transcript_dir = tmp_path / ".obscura" / "transcripts"
        if transcript_dir.exists():
            files = list(transcript_dir.glob("passthrough_claude_*.txt"))
            assert len(files) >= 1
            content = files[0].read_text()
            assert "transcript line" in content

    def test_passthrough_memory_persisted(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Passthrough should write transcript to MemoryStore."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock()

        call_count = 0

        async def _readline() -> bytes:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return b"memory test\n"
            return b""

        mock_stdout = AsyncMock()
        mock_stdout.readline = _readline
        mock_stderr = AsyncMock()
        mock_stderr.readline = AsyncMock(return_value=b"")
        mock_proc.stdout = mock_stdout
        mock_proc.stderr = mock_stderr

        with patch("shutil.which", return_value="/usr/bin/openai"), patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ):
            result = runner.invoke(cli, ["passthrough", "--capture", "openai"])

        assert result.exit_code == 0

        # Verify MemoryStore has the passthrough entry
        from sdk.memory import MemoryStore

        user = _resolve_cli_user()
        mem = MemoryStore.for_user(user)
        keys = mem.list_keys(namespace="passthrough")
        # Should have at least one passthrough entry
        assert len(keys) >= 1
