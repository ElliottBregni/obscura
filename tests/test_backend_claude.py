"""Tests for sdk.backends.claude — ClaudeBackend."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sdk._auth import AuthConfig
from sdk._types import Backend, HookPoint


def _make_auth(**kw):
    return AuthConfig(anthropic_api_key=kw.get("api_key", "sk-ant-test"))


class TestClaudeBackendInit:
    def test_defaults(self):
        from sdk.backends.claude import ClaudeBackend
        b = ClaudeBackend(_make_auth())
        assert b._model == "claude-sonnet-4-5-20250929"
        assert b._permission_mode == "default"
        assert b._cwd is None
        assert b._client is None

    def test_custom_settings(self):
        from sdk.backends.claude import ClaudeBackend
        b = ClaudeBackend(
            _make_auth(),
            model="claude-3-haiku",
            system_prompt="Be brief",
            permission_mode="strict",
            cwd="/tmp",
        )
        assert b._model == "claude-3-haiku"
        assert b._system_prompt == "Be brief"
        assert b._permission_mode == "strict"
        assert b._cwd == "/tmp"


class TestClaudeLifecycle:
    @pytest.mark.asyncio
    async def test_start(self):
        from sdk.backends.claude import ClaudeBackend
        b = ClaudeBackend(_make_auth())
        mock_client = AsyncMock()
        # ClaudeSDKClient is imported locally from claude_agent_sdk
        with patch("claude_agent_sdk.ClaudeSDKClient", return_value=mock_client), \
             patch("claude_agent_sdk.ClaudeAgentOptions"):
            await b.start()
            mock_client.connect.assert_awaited_once()
            assert b._client is mock_client

    @pytest.mark.asyncio
    async def test_stop(self):
        from sdk.backends.claude import ClaudeBackend
        b = ClaudeBackend(_make_auth())
        b._client = AsyncMock()
        await b.stop()
        assert b._client is None

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self):
        from sdk.backends.claude import ClaudeBackend
        b = ClaudeBackend(_make_auth())
        await b.stop()  # Should not raise


class TestClaudeSend:
    @pytest.mark.asyncio
    async def test_send(self):
        from sdk.backends.claude import ClaudeBackend
        b = ClaudeBackend(_make_auth())
        mock_client = AsyncMock()

        # Build mock messages that _to_message can parse
        assistant_msg = MagicMock()
        type(assistant_msg).__name__ = "AssistantMessage"
        text_block = MagicMock()
        type(text_block).__name__ = "TextBlock"
        text_block.text = "Claude says hello"
        assistant_msg.content = [text_block]

        result_msg = MagicMock()
        type(result_msg).__name__ = "ResultMessage"
        result_msg.session_id = "sess-1"

        call_count = 0

        async def mock_receive():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: drain — yield nothing
                return
                yield  # Make it an async generator
            else:
                # Second call: actual response
                yield assistant_msg
                yield result_msg

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()
        b._client = mock_client

        msg = await b.send("Hello")
        assert msg.content[0].text == "Claude says hello"

    @pytest.mark.asyncio
    async def test_send_not_started(self):
        from sdk.backends.claude import ClaudeBackend
        b = ClaudeBackend(_make_auth())
        with pytest.raises(RuntimeError, match="not started"):
            await b.send("test")


class TestClaudeSessions:
    @pytest.mark.asyncio
    async def test_create_session(self):
        from sdk.backends.claude import ClaudeBackend
        b = ClaudeBackend(_make_auth())
        b._client = AsyncMock()

        # create_session calls query() then iterates receive_response()
        result_msg = MagicMock()
        type(result_msg).__name__ = "ResultMessage"
        result_msg.session_id = "claude-sess-1"

        async def mock_receive():
            yield result_msg

        b._client.receive_response = mock_receive
        b._client.query = AsyncMock()

        ref = await b.create_session()
        assert ref.session_id == "claude-sess-1"
        assert ref.backend == Backend.CLAUDE

    @pytest.mark.asyncio
    async def test_list_sessions(self):
        from sdk.backends.claude import ClaudeBackend
        b = ClaudeBackend(_make_auth())
        b._client = AsyncMock()

        # First create a session so there's something to list
        result_msg = MagicMock()
        type(result_msg).__name__ = "ResultMessage"
        result_msg.session_id = "s1"

        async def mock_receive():
            yield result_msg

        b._client.receive_response = mock_receive
        b._client.query = AsyncMock()

        await b.create_session()

        refs = await b.list_sessions()
        assert len(refs) == 1

    @pytest.mark.asyncio
    async def test_delete_session(self):
        from sdk.backends.claude import ClaudeBackend
        from sdk._types import SessionRef
        b = ClaudeBackend(_make_auth())
        b._client = AsyncMock()

        # delete_session just removes from session store, doesn't call client
        # First add a session to the store
        ref = SessionRef(session_id="s1", backend=Backend.CLAUDE)
        b._session_store.add(ref)

        await b.delete_session(ref)
        # After deletion, listing should be empty
        refs = await b.list_sessions()
        assert len(refs) == 0


class TestClaudeTools:
    def test_register_tool(self):
        from sdk.backends.claude import ClaudeBackend
        from sdk._types import ToolSpec
        b = ClaudeBackend(_make_auth())
        spec = ToolSpec(name="t1", description="test tool", parameters={}, handler=lambda: None)
        b.register_tool(spec)
        assert len(b._tools) == 1

    def test_register_hook(self):
        from sdk.backends.claude import ClaudeBackend
        b = ClaudeBackend(_make_auth())
        cb = MagicMock()
        b.register_hook(HookPoint.STOP, cb)
        assert cb in b._hooks[HookPoint.STOP]


class TestClaudeInternals:
    def test_ensure_client_raises(self):
        from sdk.backends.claude import ClaudeBackend
        b = ClaudeBackend(_make_auth())
        with pytest.raises(RuntimeError, match="not started"):
            b._ensure_client()

    def test_to_message_empty(self):
        from sdk.backends.claude import ClaudeBackend
        b = ClaudeBackend(_make_auth())
        msg = b._to_message([])
        assert msg.role.value == "assistant"
        assert msg.content[0].text == ""

    def test_to_message_with_assistant(self):
        from sdk.backends.claude import ClaudeBackend
        b = ClaudeBackend(_make_auth())

        assistant_msg = MagicMock()
        type(assistant_msg).__name__ = "AssistantMessage"
        text_block = MagicMock()
        type(text_block).__name__ = "TextBlock"
        text_block.text = "Hello there"
        assistant_msg.content = [text_block]

        msg = b._to_message([assistant_msg])
        assert msg.content[0].text == "Hello there"
        assert msg.backend == Backend.CLAUDE

    def test_to_message_with_thinking(self):
        from sdk.backends.claude import ClaudeBackend
        b = ClaudeBackend(_make_auth())

        assistant_msg = MagicMock()
        type(assistant_msg).__name__ = "AssistantMessage"
        thinking_block = MagicMock()
        type(thinking_block).__name__ = "ThinkingBlock"
        thinking_block.thinking = "Let me think..."
        assistant_msg.content = [thinking_block]

        msg = b._to_message([assistant_msg])
        assert msg.content[0].kind == "thinking"
        assert msg.content[0].text == "Let me think..."

    def test_to_message_with_tool_use(self):
        from sdk.backends.claude import ClaudeBackend
        b = ClaudeBackend(_make_auth())

        assistant_msg = MagicMock()
        type(assistant_msg).__name__ = "AssistantMessage"
        tool_block = MagicMock()
        type(tool_block).__name__ = "ToolUseBlock"
        tool_block.name = "my_tool"
        tool_block.input = {"key": "val"}
        tool_block.id = "tu_123"
        assistant_msg.content = [tool_block]

        msg = b._to_message([assistant_msg])
        assert msg.content[0].kind == "tool_use"
        assert msg.content[0].tool_name == "my_tool"
