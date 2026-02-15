"""Tests for sdk.backends.copilot — CopilotBackend."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sdk.internal.auth import AuthConfig
from sdk.internal.types import Backend, HookPoint, ToolSpec
from sdk.backends.copilot import (
    CopilotBackend,
    public_make_handler,
    public_get_backend_tracer,
    public_set_span_attr,
)


def _make_auth(github_token: str = "gh-tok") -> AuthConfig:
    return AuthConfig(github_token=github_token)


class TestCopilotBackendInit:
    def test_defaults(self):
        b = CopilotBackend(_make_auth())
        assert b.model is None
        assert b.system_prompt == ""
        assert b.streaming is True
        assert b.client is None

    def test_with_model_and_prompt(self):
        b = CopilotBackend(_make_auth(), model="gpt-4o", system_prompt="Be helpful")
        assert b.model == "gpt-4o"
        assert b.system_prompt == "Be helpful"


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
            assert b.client is mock_client
            assert b.session is mock_session

    @pytest.mark.asyncio
    async def test_stop(self):
        b = CopilotBackend(_make_auth())
        mock_client = AsyncMock()
        b.set_client_for_testing(mock_client)
        b.set_session_for_testing(MagicMock())
        await b.stop()
        mock_client.stop.assert_awaited_once()
        assert b.client is None
        assert b.session is None

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self):
        b = CopilotBackend(_make_auth())
        await b.stop()  # Should not raise


class TestCopilotBackendSend:
    @pytest.mark.asyncio
    async def test_send(self):
        b = CopilotBackend(_make_auth())
        b.set_client_for_testing(MagicMock())

        mock_response = MagicMock()
        mock_response.data.content = "Hello back!"
        mock_session = AsyncMock()
        mock_session.send_and_wait.return_value = mock_response
        b.set_session_for_testing(mock_session)

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
        b.set_client_for_testing(AsyncMock())
        mock_session = MagicMock()
        mock_session.session_id = "sess-1"
        b.client.create_session.return_value = mock_session

        ref = await b.create_session()
        assert ref.session_id == "sess-1"
        assert ref.backend == Backend.COPILOT

    @pytest.mark.asyncio
    async def test_resume_session(self):
        b = CopilotBackend(_make_auth())
        b.set_client_for_testing(AsyncMock())
        from sdk.internal.types import SessionRef

        ref = SessionRef(session_id="s1", backend=Backend.COPILOT)
        await b.resume_session(ref)
        b.client.resume_session.assert_awaited_once_with("s1")

    @pytest.mark.asyncio
    async def test_list_sessions(self):
        b = CopilotBackend(_make_auth())
        b.set_client_for_testing(AsyncMock())
        mock_sess = MagicMock()
        mock_sess.session_id = "s1"
        b.client.list_sessions.return_value = [mock_sess]

        refs = await b.list_sessions()
        assert len(refs) == 1
        assert refs[0].session_id == "s1"

    @pytest.mark.asyncio
    async def test_delete_session(self):
        b = CopilotBackend(_make_auth())
        b.set_client_for_testing(AsyncMock())
        from sdk.internal.types import SessionRef

        ref = SessionRef(session_id="s1", backend=Backend.COPILOT)
        await b.delete_session(ref)
        b.client.delete_session.assert_awaited_once_with("s1")


class TestCopilotBackendTools:
    def test_register_tool(self):
        b = CopilotBackend(_make_auth())
        spec = ToolSpec(
            name="test_tool",
            description="A test tool",
            parameters={},
            handler=lambda: None,
        )
        b.register_tool(spec)
        assert len(b.tools) == 1
        assert b.get_tool_registry() is b.tool_registry


class TestCopilotBackendHooks:
    def test_register_hook(self):
        b = CopilotBackend(_make_auth())
        cb = MagicMock()
        b.register_hook(HookPoint.PRE_TOOL_USE, cb)
        assert cb in b.hooks[HookPoint.PRE_TOOL_USE]


class TestCopilotBackendInternals:
    def test_ensure_client_raises(self):
        b = CopilotBackend(_make_auth())
        with pytest.raises(RuntimeError, match="not started"):
            b.ensure_client_started()

    def test_ensure_session_raises(self):
        b = CopilotBackend(_make_auth())
        b.set_client_for_testing(MagicMock())
        with pytest.raises(RuntimeError, match="No active session"):
            b.ensure_session_started()

    def test_build_session_config(self):
        b = CopilotBackend(_make_auth(), model="gpt-4o", system_prompt="test")
        config = b.build_session_config()
        assert config["model"] == "gpt-4o"
        assert config["system_message"]["content"] == "test"
        assert config["streaming"] is True

    def test_build_hooks_config_empty(self):
        b = CopilotBackend(_make_auth())
        assert b.build_hooks_config() is None

    def test_build_hooks_config_single(self):
        b = CopilotBackend(_make_auth())
        cb = MagicMock()
        b.register_hook(HookPoint.PRE_TOOL_USE, cb)
        config = b.build_hooks_config()
        assert config is not None
        assert "on_pre_tool_use" in config

    def test_to_message_from_data_content(self):
        b = CopilotBackend(_make_auth())
        raw = MagicMock()
        raw.data.content = "hello"
        msg = b.to_message(raw)
        assert msg.content[0].text == "hello"

    def test_to_message_from_str(self):
        b = CopilotBackend(_make_auth())
        msg = b.to_message("plain text")
        assert msg.content[0].text == "plain text"


class TestHelpers:
    def test_make_handler_filters_type(self):
        cb = MagicMock()
        handler = public_make_handler("assistant.message_delta", cb)

        event = MagicMock()
        event.type = "assistant.message_delta"
        handler(event)
        cb.assert_called_once()

    def test_make_handler_ignores_wrong_type(self):
        cb = MagicMock()
        handler = public_make_handler("assistant.message_delta", cb)

        event = MagicMock()
        event.type = "session.idle"
        handler(event)
        cb.assert_not_called()

    def test_get_backend_tracer_fallback(self):
        tracer = public_get_backend_tracer()
        assert tracer is not None

    def test_set_span_attr_no_op(self):
        span = MagicMock(spec=[])
        public_set_span_attr(span, "key", "val")  # Should not raise

    def test_set_span_attr_works(self):
        span = MagicMock()
        public_set_span_attr(span, "key", "val")
        span.set_attribute.assert_called_once_with("key", "val")
