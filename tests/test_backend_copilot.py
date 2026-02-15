"""Tests for sdk.backends.copilot — CopilotBackend."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sdk._auth import AuthConfig
from sdk._types import Backend, HookPoint, ToolSpec
from sdk.backends.copilot import CopilotBackend, _make_handler, _get_backend_tracer, _set_span_attr


def _make_auth(**kw):
    return AuthConfig(github_token=kw.get("github_token", "gh-tok"))


class TestCopilotBackendInit:
    def test_defaults(self):
        b = CopilotBackend(_make_auth())
        assert b._model is None
        assert b._system_prompt == ""
        assert b._streaming is True
        assert b._client is None

    def test_with_model_and_prompt(self):
        b = CopilotBackend(_make_auth(), model="gpt-4o", system_prompt="Be helpful")
        assert b._model == "gpt-4o"
        assert b._system_prompt == "Be helpful"


class TestCopilotBackendLifecycle:
    @pytest.mark.asyncio
    async def test_start(self):
        b = CopilotBackend(_make_auth())
        mock_client = AsyncMock()
        mock_session = MagicMock()
        mock_client.create_session.return_value = mock_session

        with patch("copilot.CopilotClient", return_value=mock_client):
            await b.start()
            mock_client.start.assert_awaited_once()
            assert b._client is mock_client
            assert b._session is mock_session

    @pytest.mark.asyncio
    async def test_stop(self):
        b = CopilotBackend(_make_auth())
        mock_client = AsyncMock()
        b._client = mock_client
        b._session = MagicMock()
        await b.stop()
        mock_client.stop.assert_awaited_once()
        assert b._client is None
        assert b._session is None

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self):
        b = CopilotBackend(_make_auth())
        await b.stop()  # Should not raise


class TestCopilotBackendSend:
    @pytest.mark.asyncio
    async def test_send(self):
        b = CopilotBackend(_make_auth())
        b._client = MagicMock()

        mock_response = MagicMock()
        mock_response.data.content = "Hello back!"
        mock_session = AsyncMock()
        mock_session.send_and_wait.return_value = mock_response
        b._session = mock_session

        msg = await b.send("Hello")
        assert msg.content[0].text == "Hello back!"
        assert msg.role.value == "assistant"

    @pytest.mark.asyncio
    async def test_send_not_started(self):
        b = CopilotBackend(_make_auth())
        with pytest.raises(RuntimeError, match="not started"):
            await b.send("test")


class TestCopilotBackendSessions:
    @pytest.mark.asyncio
    async def test_create_session(self):
        b = CopilotBackend(_make_auth())
        b._client = AsyncMock()
        mock_session = MagicMock()
        mock_session.session_id = "sess-1"
        b._client.create_session.return_value = mock_session

        ref = await b.create_session()
        assert ref.session_id == "sess-1"
        assert ref.backend == Backend.COPILOT

    @pytest.mark.asyncio
    async def test_resume_session(self):
        b = CopilotBackend(_make_auth())
        b._client = AsyncMock()
        from sdk._types import SessionRef
        ref = SessionRef(session_id="s1", backend=Backend.COPILOT)
        await b.resume_session(ref)
        b._client.resume_session.assert_awaited_once_with("s1")

    @pytest.mark.asyncio
    async def test_list_sessions(self):
        b = CopilotBackend(_make_auth())
        b._client = AsyncMock()
        mock_sess = MagicMock()
        mock_sess.session_id = "s1"
        b._client.list_sessions.return_value = [mock_sess]

        refs = await b.list_sessions()
        assert len(refs) == 1
        assert refs[0].session_id == "s1"

    @pytest.mark.asyncio
    async def test_delete_session(self):
        b = CopilotBackend(_make_auth())
        b._client = AsyncMock()
        from sdk._types import SessionRef
        ref = SessionRef(session_id="s1", backend=Backend.COPILOT)
        await b.delete_session(ref)
        b._client.delete_session.assert_awaited_once_with("s1")


class TestCopilotBackendTools:
    def test_register_tool(self):
        b = CopilotBackend(_make_auth())
        spec = ToolSpec(name="test_tool", description="A test tool", parameters={}, handler=lambda: None)
        b.register_tool(spec)
        assert len(b._tools) == 1
        assert b.get_tool_registry() is b._tool_registry


class TestCopilotBackendHooks:
    def test_register_hook(self):
        b = CopilotBackend(_make_auth())
        cb = MagicMock()
        b.register_hook(HookPoint.PRE_TOOL_USE, cb)
        assert cb in b._hooks[HookPoint.PRE_TOOL_USE]


class TestCopilotBackendInternals:
    def test_ensure_client_raises(self):
        b = CopilotBackend(_make_auth())
        with pytest.raises(RuntimeError, match="not started"):
            b._ensure_client()

    def test_ensure_session_raises(self):
        b = CopilotBackend(_make_auth())
        b._client = MagicMock()
        with pytest.raises(RuntimeError, match="No active session"):
            b._ensure_session()

    def test_build_session_config(self):
        b = CopilotBackend(_make_auth(), model="gpt-4o", system_prompt="test")
        config = b._build_session_config()
        assert config["model"] == "gpt-4o"
        assert config["system_message"]["content"] == "test"
        assert config["streaming"] is True

    def test_build_hooks_config_empty(self):
        b = CopilotBackend(_make_auth())
        assert b._build_hooks_config() is None

    def test_build_hooks_config_single(self):
        b = CopilotBackend(_make_auth())
        cb = MagicMock()
        b.register_hook(HookPoint.PRE_TOOL_USE, cb)
        config = b._build_hooks_config()
        assert "on_pre_tool_use" in config

    def test_to_message_from_data_content(self):
        b = CopilotBackend(_make_auth())
        raw = MagicMock()
        raw.data.content = "hello"
        msg = b._to_message(raw)
        assert msg.content[0].text == "hello"

    def test_to_message_from_str(self):
        b = CopilotBackend(_make_auth())
        msg = b._to_message("plain text")
        assert msg.content[0].text == "plain text"


class TestHelpers:
    def test_make_handler_filters_type(self):
        cb = MagicMock()
        handler = _make_handler("assistant.message_delta", cb)

        event = MagicMock()
        event.type = "assistant.message_delta"
        handler(event)
        cb.assert_called_once()

    def test_make_handler_ignores_wrong_type(self):
        cb = MagicMock()
        handler = _make_handler("assistant.message_delta", cb)

        event = MagicMock()
        event.type = "session.idle"
        handler(event)
        cb.assert_not_called()

    def test_get_backend_tracer_fallback(self):
        tracer = _get_backend_tracer()
        assert tracer is not None

    def test_set_span_attr_no_op(self):
        span = MagicMock(spec=[])
        _set_span_attr(span, "key", "val")  # Should not raise

    def test_set_span_attr_works(self):
        span = MagicMock()
        _set_span_attr(span, "key", "val")
        span.set_attribute.assert_called_once_with("key", "val")
