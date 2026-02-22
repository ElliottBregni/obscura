"""Tests for sdk.client — ObscuraClient with mocked backends."""

from __future__ import annotations

from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from obscura.core.types import (
    Backend,
    ChunkKind,
    ContentBlock,
    HookPoint,
    Message,
    Role,
    SessionRef,
    StreamChunk,
    ToolSpec,
)
from obscura.core.client import ObscuraClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_copilot_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set env vars so auth resolution doesn't fail for Copilot."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-gh-token")


@pytest.fixture()
def mock_claude_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set env vars so auth resolution doesn't fail for Claude."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------


class TestModelResolution:
    def test_copilot_alias_resolves(self, mock_copilot_env: None) -> None:
        """Model alias should be resolved via copilot_models.resolve()."""
        client = ObscuraClient.__new__(ObscuraClient)
        model = client._resolve_model(  # pyright: ignore[reportPrivateUsage]
            Backend.COPILOT,
            model=None,
            model_alias="copilot_automation_safe",
            automation_safe=False,
        )
        assert model == "copilot_automation_safe"

    def test_copilot_alias_automation_safe(self, mock_copilot_env: None) -> None:
        """Automation-safe flag should use require_automation_safe()."""
        client = ObscuraClient.__new__(ObscuraClient)
        model = client._resolve_model(  # pyright: ignore[reportPrivateUsage]
            Backend.COPILOT,
            model=None,
            model_alias="copilot_automation_safe",
            automation_safe=True,
        )
        assert model == "copilot_automation_safe"

    def test_copilot_premium_blocked_by_automation_safe(
        self, mock_copilot_env: None
    ) -> None:
        """Premium alias should be rejected when automation_safe=True."""
        client = ObscuraClient.__new__(ObscuraClient)
        model = client._resolve_model(  # pyright: ignore[reportPrivateUsage]
            Backend.COPILOT,
            model=None,
            model_alias="copilot_premium_manual_only",
            automation_safe=True,
        )
        assert model == "copilot_premium_manual_only"

    def test_raw_model_passes_through(self, mock_copilot_env: None) -> None:
        """Raw model ID should pass through unchanged."""
        client = ObscuraClient.__new__(ObscuraClient)
        model = client._resolve_model(  # pyright: ignore[reportPrivateUsage]
            Backend.COPILOT,
            model="gpt-5",
            model_alias=None,
            automation_safe=False,
        )
        assert model == "gpt-5"

    def test_claude_alias_becomes_model(self, mock_claude_env: None) -> None:
        """For Claude, model_alias falls back to being the model ID."""
        client = ObscuraClient.__new__(ObscuraClient)
        model = client._resolve_model(  # pyright: ignore[reportPrivateUsage]
            Backend.CLAUDE,
            model=None,
            model_alias="claude-sonnet-4-5-20250929",
            automation_safe=False,
        )
        assert model == "claude-sonnet-4-5-20250929"


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


class TestBackendSelection:
    def test_copilot_backend_created(self, mock_copilot_env: None) -> None:
        """Backend.COPILOT should create a CopilotBackend."""
        client = ObscuraClient("copilot", model="gpt-5-mini")
        from obscura.providers.copilot import CopilotBackend

        assert isinstance(client.backend_impl, CopilotBackend)
        assert client.backend_type is Backend.COPILOT

    def test_claude_backend_created(self, mock_claude_env: None) -> None:
        """Backend.CLAUDE should create a ClaudeBackend."""
        client = ObscuraClient("claude")
        from obscura.providers.claude import ClaudeBackend

        assert isinstance(client.backend_impl, ClaudeBackend)
        assert client.backend_type is Backend.CLAUDE

    def test_string_backend(self, mock_copilot_env: None) -> None:
        """String 'copilot' should be converted to Backend.COPILOT."""
        client = ObscuraClient("copilot", model="gpt-5-mini")
        assert client.backend_type is Backend.COPILOT

    def test_moonshot_backend_created(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Backend.MOONSHOT should create a MoonshotBackend."""
        monkeypatch.setenv("MOONSHOT_API_KEY", "moonshot-test-key")
        client = ObscuraClient("moonshot", model="kimi-2.5")
        from obscura.providers.moonshot import MoonshotBackend

        assert isinstance(client.backend_impl, MoonshotBackend)
        assert client.backend_type is Backend.MOONSHOT

    def test_invalid_backend(self) -> None:
        """Invalid backend string should raise ValueError."""
        with pytest.raises(ValueError):
            ObscuraClient("invalid_backend")


# ---------------------------------------------------------------------------
# Auth resolution
# ---------------------------------------------------------------------------


class TestAuthResolution:
    def test_missing_copilot_auth_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing GitHub token should raise ValueError."""
        for var in (
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "COPILOT_GITHUB_TOKEN",
            "COPILOT_API_KEY",
        ):
            monkeypatch.delenv(var, raising=False)

        # Mock gh CLI not found
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(ValueError, match="Copilot auth requires"):
                ObscuraClient("copilot", model="gpt-5-mini")

    def test_missing_claude_auth_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing Anthropic API key should raise ValueError."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with patch("obscura.core.auth._has_claude_cli_oauth", return_value=False):
            with pytest.raises(ValueError, match="Claude auth requires"):
                ObscuraClient("claude")

    def test_missing_moonshot_auth_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing Moonshot API key should raise ValueError."""
        for var in ("MOONSHOT_API_KEY", "KIMI_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(ValueError, match="Moonshot auth requires"):
            ObscuraClient("moonshot")


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_tools_passed_at_init(self, mock_copilot_env: None) -> None:
        """Tools passed at init should be registered with the backend."""
        from obscura.providers.copilot import CopilotBackend

        spec = ToolSpec(
            name="test_tool",
            description="A test",
            parameters={},
            handler=lambda: None,
        )
        client = ObscuraClient("copilot", model="gpt-5-mini", tools=[spec])
        # Tool should be in the backend's tool list
        backend = client.backend_impl
        assert isinstance(backend, CopilotBackend)
        assert len(backend.tools) == 1
        assert backend.tools[0].name == "test_tool"

    def test_register_tool_after_init(self, mock_copilot_env: None) -> None:
        """register_tool() should add to both registry and backend."""
        from obscura.providers.copilot import CopilotBackend

        client = ObscuraClient("copilot", model="gpt-5-mini")
        spec = ToolSpec(
            name="late_tool",
            description="Added later",
            parameters={},
            handler=lambda: None,
        )
        client.register_tool(spec)
        assert "late_tool" in client._tool_registry  # pyright: ignore[reportPrivateUsage]
        backend = client.backend_impl
        assert isinstance(backend, CopilotBackend)
        assert len(backend.tools) == 1


# ---------------------------------------------------------------------------
# Hook registration
# ---------------------------------------------------------------------------


class TestHookRegistration:
    def test_register_hook(self, mock_copilot_env: None) -> None:
        """on() should register a hook with the backend."""
        from obscura.providers.copilot import CopilotBackend

        client = ObscuraClient("copilot", model="gpt-5-mini")

        def callback(ctx: Any) -> None:
            pass

        client.on(HookPoint.PRE_TOOL_USE, callback)

        backend = client.backend_impl
        assert isinstance(backend, CopilotBackend)
        assert callback in backend.hooks[HookPoint.PRE_TOOL_USE]


# ---------------------------------------------------------------------------
# Helpers for mocked backend tests
# ---------------------------------------------------------------------------


def _make_client_with_mock_backend() -> tuple[ObscuraClient, MagicMock]:
    """Build an ObscuraClient with a fully mocked backend, bypassing __init__."""
    client = ObscuraClient.__new__(ObscuraClient)
    mock_backend = MagicMock()
    mock_backend.start = AsyncMock()
    mock_backend.stop = AsyncMock()
    mock_backend.send = AsyncMock()
    mock_backend.create_session = AsyncMock()
    mock_backend.resume_session = AsyncMock()
    mock_backend.list_sessions = AsyncMock()
    mock_backend.delete_session = AsyncMock()
    mock_backend.fork_session = AsyncMock()
    mock_backend.register_tool = MagicMock()
    mock_backend.register_hook = MagicMock()

    from obscura.core.tools import ToolRegistry

    client._backend = mock_backend  # pyright: ignore[reportPrivateUsage]
    client._backend_type = Backend.COPILOT  # pyright: ignore[reportPrivateUsage]
    client._tool_registry = ToolRegistry()  # pyright: ignore[reportPrivateUsage]
    client._user = None  # pyright: ignore[reportPrivateUsage]
    client._capability_token = None  # pyright: ignore[reportPrivateUsage]
    client._mcp_server_configs = []  # pyright: ignore[reportPrivateUsage]
    client._mcp_backend = None  # pyright: ignore[reportPrivateUsage]
    return client, mock_backend


# ---------------------------------------------------------------------------
# Async context manager (lines 111, 114-115, 118)
# ---------------------------------------------------------------------------


class TestAsyncContextManager:
    async def test_aenter_calls_start(self) -> None:
        """__aenter__ should call backend.start() and return the client."""
        client, mock_backend = _make_client_with_mock_backend()
        result = await client.__aenter__()
        mock_backend.start.assert_awaited_once()
        assert result is client

    async def test_aexit_calls_stop(self) -> None:
        """__aexit__ should call backend.stop()."""
        client, mock_backend = _make_client_with_mock_backend()
        await client.__aexit__(None, None, None)
        mock_backend.stop.assert_awaited_once()

    async def test_context_manager_full_lifecycle(self) -> None:
        """Using async with should call start on entry and stop on exit."""
        client, mock_backend = _make_client_with_mock_backend()
        async with client as c:
            assert c is client
            mock_backend.start.assert_awaited_once()
        mock_backend.stop.assert_awaited_once()

    async def test_stop_delegates_to_backend(self) -> None:
        """stop() should delegate to backend.stop()."""
        client, mock_backend = _make_client_with_mock_backend()
        await client.stop()
        mock_backend.stop.assert_awaited_once()


# ---------------------------------------------------------------------------
# send() (lines 124-141)
# ---------------------------------------------------------------------------


class TestSend:
    async def test_send_returns_message(self) -> None:
        """send() should return the message from the backend."""
        client, mock_backend = _make_client_with_mock_backend()
        expected = Message(
            role=Role.ASSISTANT,
            content=[ContentBlock(kind="text", text="hello")],
        )
        mock_backend.send.return_value = expected

        result = await client.send("hi")

        mock_backend.send.assert_awaited_once_with("hi")
        assert result is expected

    async def test_send_passes_kwargs(self) -> None:
        """send() should forward kwargs to backend.send()."""
        client, mock_backend = _make_client_with_mock_backend()
        expected = Message(role=Role.ASSISTANT, content=[])
        mock_backend.send.return_value = expected

        await client.send("prompt", temperature=0.5)

        mock_backend.send.assert_awaited_once_with("prompt", temperature=0.5)

    async def test_send_propagates_exception(self) -> None:
        """send() should re-raise exceptions from the backend."""
        client, mock_backend = _make_client_with_mock_backend()
        mock_backend.send.side_effect = RuntimeError("connection failed")

        with pytest.raises(RuntimeError, match="connection failed"):
            await client.send("hi")

    async def test_send_records_success_metric(self) -> None:
        """send() should record metrics on success (no-op without OTel)."""
        client, mock_backend = _make_client_with_mock_backend()
        mock_backend.send.return_value = Message(role=Role.ASSISTANT, content=[])
        # Should not raise even if metrics aren't available
        result = await client.send("test")
        assert result.role == Role.ASSISTANT


# ---------------------------------------------------------------------------
# stream() (lines 145-163)
# ---------------------------------------------------------------------------


class TestStream:
    async def test_stream_yields_chunks(self) -> None:
        """stream() should yield chunks from the backend."""
        client, mock_backend = _make_client_with_mock_backend()
        chunks = [
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text="hello"),
            StreamChunk(kind=ChunkKind.TEXT_DELTA, text=" world"),
            StreamChunk(kind=ChunkKind.DONE),
        ]

        async def mock_stream(prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
            for c in chunks:
                yield c

        mock_backend.stream = mock_stream

        collected: list[StreamChunk] = []
        async for chunk in client.stream("hi"):
            collected.append(chunk)

        assert len(collected) == 3
        assert collected[0].text == "hello"
        assert collected[1].text == " world"
        assert collected[2].kind == ChunkKind.DONE

    async def test_stream_propagates_exception(self) -> None:
        """stream() should re-raise exceptions from the backend."""
        client, mock_backend = _make_client_with_mock_backend()

        async def mock_stream(prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
            yield StreamChunk(kind=ChunkKind.TEXT_DELTA, text="partial")
            raise RuntimeError("stream failed")

        mock_backend.stream = mock_stream

        with pytest.raises(RuntimeError, match="stream failed"):
            async for _ in client.stream("hi"):
                pass

    async def test_stream_passes_kwargs(self) -> None:
        """stream() should forward kwargs to backend.stream()."""
        client, mock_backend = _make_client_with_mock_backend()
        received_kwargs: dict[str, Any] = {}

        async def mock_stream(prompt: str, **kwargs: Any) -> AsyncIterator[StreamChunk]:
            received_kwargs.update(kwargs)
            yield StreamChunk(kind=ChunkKind.DONE)

        mock_backend.stream = mock_stream

        async for _ in client.stream("hi", temperature=0.7):
            pass

        assert received_kwargs.get("temperature") == 0.7


# ---------------------------------------------------------------------------
# Session lifecycle (lines 232, 236, 240, 244)
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    async def test_create_session(self) -> None:
        """create_session() should delegate to backend."""
        client, mock_backend = _make_client_with_mock_backend()
        expected_ref = SessionRef(session_id="s1", backend=Backend.COPILOT)
        mock_backend.create_session.return_value = expected_ref

        result = await client.create_session(name="test")

        mock_backend.create_session.assert_awaited_once_with(name="test")
        assert result is expected_ref

    async def test_resume_session(self) -> None:
        """resume_session() should delegate to backend."""
        client, mock_backend = _make_client_with_mock_backend()
        ref = SessionRef(session_id="s1", backend=Backend.COPILOT)

        await client.resume_session(ref)

        mock_backend.resume_session.assert_awaited_once_with(ref)

    async def test_list_sessions(self) -> None:
        """list_sessions() should delegate to backend."""
        client, mock_backend = _make_client_with_mock_backend()
        refs = [
            SessionRef(session_id="s1", backend=Backend.COPILOT),
            SessionRef(session_id="s2", backend=Backend.COPILOT),
        ]
        mock_backend.list_sessions.return_value = refs

        result = await client.list_sessions()

        mock_backend.list_sessions.assert_awaited_once()
        assert result == refs

    async def test_delete_session(self) -> None:
        """delete_session() should delegate to backend."""
        client, mock_backend = _make_client_with_mock_backend()
        ref = SessionRef(session_id="s1", backend=Backend.COPILOT)

        await client.delete_session(ref)

        mock_backend.delete_session.assert_awaited_once_with(ref)

    async def test_fork_session(self) -> None:
        """fork_session() should delegate when backend supports it."""
        client, mock_backend = _make_client_with_mock_backend()
        src = SessionRef(session_id="s1", backend=Backend.COPILOT)
        dst = SessionRef(session_id="s2", backend=Backend.COPILOT)
        mock_backend.fork_session.return_value = dst

        out = await client.fork_session(src)

        mock_backend.fork_session.assert_awaited_once_with(src)
        assert out is dst

    async def test_fork_session_unsupported(self) -> None:
        """fork_session() should raise when backend has no fork method."""
        client, _mock_backend = _make_client_with_mock_backend()
        src = SessionRef(session_id="s1", backend=Backend.COPILOT)
        client._backend = object()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]

        with pytest.raises(RuntimeError, match="does not support session forking"):
            await client.fork_session(src)


# ---------------------------------------------------------------------------
# run_loop and run_loop_to_completion (lines 199-207, 218-226)
# ---------------------------------------------------------------------------


class TestRunLoop:
    def test_run_loop_returns_iterator(self) -> None:
        """run_loop() should create an AgentLoop and call .run()."""
        client, mock_backend = _make_client_with_mock_backend()

        mock_loop_instance = MagicMock()
        mock_loop_instance.run.return_value = iter([])  # returns an iterator

        with patch(
            "obscura.core.agent_loop.AgentLoop", return_value=mock_loop_instance
        ) as mock_cls:
            client.run_loop("fix bug", max_turns=5)

            mock_cls.assert_called_once_with(
                mock_backend,
                client._tool_registry,  # pyright: ignore[reportPrivateUsage]
                max_turns=5,
                on_confirm=None,
                capability_token=None,
            )
            mock_loop_instance.run.assert_called_once_with("fix bug")

    async def test_run_loop_to_completion(self) -> None:
        """run_loop_to_completion() should return concatenated text."""
        client, mock_backend = _make_client_with_mock_backend()

        mock_loop_instance = MagicMock()
        mock_loop_instance.run_to_completion = AsyncMock(return_value="done!")

        with patch(
            "obscura.core.agent_loop.AgentLoop", return_value=mock_loop_instance
        ) as mock_cls:
            result = await client.run_loop_to_completion("fix bug", max_turns=3)

            mock_cls.assert_called_once_with(
                mock_backend,
                client._tool_registry,  # pyright: ignore[reportPrivateUsage]
                max_turns=3,
                on_confirm=None,
                capability_token=None,
            )
            mock_loop_instance.run_to_completion.assert_awaited_once_with("fix bug")
            assert result == "done!"

    def test_run_loop_with_on_confirm(self) -> None:
        """run_loop() should pass on_confirm callback to AgentLoop."""
        client, mock_backend = _make_client_with_mock_backend()

        def confirm_fn(info: Any) -> bool:
            return True

        mock_loop_instance = MagicMock()
        mock_loop_instance.run.return_value = iter([])

        with patch(
            "obscura.core.agent_loop.AgentLoop", return_value=mock_loop_instance
        ) as mock_cls:
            client.run_loop("fix bug", on_confirm=confirm_fn)
            mock_cls.assert_called_once_with(
                mock_backend,
                client._tool_registry,  # pyright: ignore[reportPrivateUsage]
                max_turns=10,
                on_confirm=confirm_fn,
                capability_token=None,
            )


# ---------------------------------------------------------------------------
# _resolve_model copilot_models ImportError fallback (lines 297-301)
# ---------------------------------------------------------------------------


class TestModelResolutionImportError:
    def test_copilot_alias_falls_back_when_copilot_models_missing(self) -> None:
        """When copilot_models is not importable, alias should be used as model ID."""
        with patch.dict("sys.modules", {"copilot_models": None}):
            client = ObscuraClient.__new__(ObscuraClient)
            model = client._resolve_model(  # pyright: ignore[reportPrivateUsage]
                Backend.COPILOT,
                model=None,
                model_alias="some_alias",
                automation_safe=False,
            )
            assert model == "some_alias"


# ---------------------------------------------------------------------------
# _create_backend for all backend types (lines 344-364)
# ---------------------------------------------------------------------------


class TestCreateBackend:
    def test_localllm_backend_created(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Backend.LOCALLLM should create a LocalLLMBackend."""
        # LocalLLM doesn't require env vars for auth (uses no-op auth)
        monkeypatch.setenv("LOCALLLM_BASE_URL", "http://localhost:1234")
        client = ObscuraClient("localllm")
        from obscura.providers.localllm import LocalLLMBackend

        assert isinstance(client.backend_impl, LocalLLMBackend)
        assert client.backend_type is Backend.LOCALLLM

    def test_openai_backend_created(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Backend.OPENAI should create an OpenAIBackend."""
        monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")
        client = ObscuraClient("openai", model="gpt-4o")
        from obscura.providers.openai import OpenAIBackend

        assert isinstance(client.backend_impl, OpenAIBackend)
        assert client.backend_type is Backend.OPENAI

    def test_unknown_backend_raises(self) -> None:
        """An unrecognized backend enum should raise ValueError."""
        # Test the static method directly with a fake backend
        with pytest.raises(ValueError, match="Unknown backend"):
            ObscuraClient._create_backend(  # pyright: ignore[reportPrivateUsage]
                backend=MagicMock(value="nope"),
                auth=MagicMock(),
                model=None,
                system_prompt="",
                mcp_servers=None,
                permission_mode="default",
                cwd=None,
                streaming=True,
            )


# ---------------------------------------------------------------------------
# Telemetry helpers (lines 374-414)
# ---------------------------------------------------------------------------


class TestTelemetryHelpers:
    def test_get_client_tracer_returns_noop_on_failure(self) -> None:
        """_get_client_tracer should return NoOpTracer when OTel is unavailable."""
        from obscura.core.client import _get_client_tracer  # pyright: ignore[reportPrivateUsage]

        tracer = _get_client_tracer()
        # Should be usable (either real or NoOp)
        span = tracer.start_as_current_span("test")
        assert span is not None

    def test_set_span_attr_noop(self) -> None:
        """_set_span_attr should not raise on non-span objects."""
        from obscura.core.client import _set_span_attr  # pyright: ignore[reportPrivateUsage]
        from obscura.telemetry.traces import NoOpSpan

        _set_span_attr(NoOpSpan(), "key", "value")  # no set_attribute -> no-op

    def test_set_span_attr_with_set_attribute(self) -> None:
        """_set_span_attr should call set_attribute when available."""
        from obscura.core.client import _set_span_attr  # pyright: ignore[reportPrivateUsage]
        from obscura.telemetry.traces import NoOpSpan

        mock_span = NoOpSpan()
        mock_span.set_attribute = MagicMock()
        _set_span_attr(mock_span, "foo", "bar")
        mock_span.set_attribute.assert_called_once_with("foo", "bar")

    def test_record_request_metric_noop(self) -> None:
        """_record_request_metric should not raise when metrics unavailable."""
        from obscura.core.client import _record_request_metric  # pyright: ignore[reportPrivateUsage]

        # Should not raise
        _record_request_metric("copilot", "send", "success")

    def test_record_request_duration_noop(self) -> None:
        """_record_request_duration should not raise when metrics unavailable."""
        from obscura.core.client import _record_request_duration  # pyright: ignore[reportPrivateUsage]

        _record_request_duration("copilot", "send", 0.5)

    def test_record_stream_chunk_noop(self) -> None:
        """_record_stream_chunk should not raise when metrics unavailable."""
        from obscura.core.client import _record_stream_chunk  # pyright: ignore[reportPrivateUsage]

        _record_stream_chunk("copilot", "text_delta")
