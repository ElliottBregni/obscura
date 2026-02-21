"""Tests for sdk.backends.claude — ClaudeBackend."""

from typing import Any, AsyncIterator
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sdk.internal.auth import AuthConfig
from sdk.internal.types import Backend, ChunkKind, HookPoint, StreamChunk, ToolChoice, ToolSpec


def _make_auth(api_key: str = "sk-ant-test") -> AuthConfig:
    return AuthConfig(anthropic_api_key=api_key)


def _tool_handler(**_: Any) -> str:
    return "ok"


class TestClaudeBackendInit:
    def test_defaults(self):
        from sdk.backends.claude import ClaudeBackend

        b = ClaudeBackend(_make_auth())
        assert b.model == "claude-sonnet-4-5-20250929"
        assert b.permission_mode == "default"
        assert b.cwd is None
        assert b.client is None

    def test_custom_settings(self):
        from sdk.backends.claude import ClaudeBackend

        b = ClaudeBackend(
            _make_auth(),
            model="claude-3-haiku",
            system_prompt="Be brief",
            permission_mode="strict",
            cwd="/tmp",
        )
        assert b.model == "claude-3-haiku"
        assert b.system_prompt == "Be brief"
        assert b.permission_mode == "strict"
        assert b.cwd == "/tmp"

    def test_capabilities_include_native_features(self):
        from sdk.backends.claude import ClaudeBackend

        b = ClaudeBackend(_make_auth())
        caps = b.capabilities()
        assert caps.supports_native_mode is True
        assert caps.supports_tool_choice is True
        assert "session_fork" in caps.native_features


class TestClaudeLifecycle:
    @pytest.mark.asyncio
    async def test_start(self):
        from sdk.backends.claude import ClaudeBackend

        b = ClaudeBackend(_make_auth())
        mock_client = AsyncMock()
        # ClaudeSDKClient is imported locally from claude_agent_sdk
        with (
            patch("claude_agent_sdk.ClaudeSDKClient", return_value=mock_client),
            patch("claude_agent_sdk.ClaudeAgentOptions"),
        ):
            await b.start()
            mock_client.connect.assert_awaited_once()
            assert b.client is mock_client

    @pytest.mark.asyncio
    async def test_stop(self):
        from sdk.backends.claude import ClaudeBackend

        b = ClaudeBackend(_make_auth())
        b.set_client_for_testing(AsyncMock())
        await b.stop()
        assert b.client is None

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
        b.set_client_for_testing(mock_client)

        msg = await b.send("Hello")
        assert msg.content[0].text == "Claude says hello"

    @pytest.mark.asyncio
    async def test_send_not_started(self):
        from sdk.backends.claude import ClaudeBackend

        b = ClaudeBackend(_make_auth())
        with pytest.raises(RuntimeError, match="not started"):
            await b.send("test")

    @pytest.mark.asyncio
    async def test_send_tool_choice_function_maps_allowed_tools(self):
        from sdk.backends.claude import ClaudeBackend

        b = ClaudeBackend(_make_auth())
        mock_client = AsyncMock()
        b.set_client_for_testing(mock_client)
        b.register_tool(
            ToolSpec(
                name="read_file",
                description="read file",
                parameters={"type": "object"},
                handler=_tool_handler,
            )
        )

        assistant_msg = MagicMock()
        type(assistant_msg).__name__ = "AssistantMessage"
        text_block = MagicMock()
        type(text_block).__name__ = "TextBlock"
        text_block.text = "ok"
        assistant_msg.content = [text_block]
        result_msg = MagicMock()
        type(result_msg).__name__ = "ResultMessage"
        result_msg.session_id = "sess-1"

        call_count = 0

        async def mock_receive():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return
                yield
            yield assistant_msg
            yield result_msg

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        await b.send("Hello", tool_choice=ToolChoice.required("read_file"))

        mock_client.query.assert_awaited_once_with(
            "Hello",
            allowed_tools=["mcp__obscura_tools__read_file"],
        )

    @pytest.mark.asyncio
    async def test_send_tool_choice_kwargs_fallback_on_type_error(self):
        from sdk.backends.claude import ClaudeBackend

        b = ClaudeBackend(_make_auth())
        mock_client = AsyncMock()
        b.set_client_for_testing(mock_client)
        b.register_tool(
            ToolSpec(
                name="read_file",
                description="read file",
                parameters={"type": "object"},
                handler=_tool_handler,
            )
        )

        assistant_msg = MagicMock()
        type(assistant_msg).__name__ = "AssistantMessage"
        text_block = MagicMock()
        type(text_block).__name__ = "TextBlock"
        text_block.text = "ok"
        assistant_msg.content = [text_block]
        result_msg = MagicMock()
        type(result_msg).__name__ = "ResultMessage"
        result_msg.session_id = "sess-1"

        call_count = 0

        async def mock_receive():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return
                yield
            yield assistant_msg
            yield result_msg

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock(side_effect=[TypeError("unsupported kw"), None])

        await b.send("Hello", tool_choice=ToolChoice.none())

        assert mock_client.query.await_count == 2
        assert mock_client.query.await_args_list[0].kwargs == {
            "disallowed_tools": ["mcp__obscura_tools__read_file"]
        }
        assert mock_client.query.await_args_list[1].args == ("Hello",)


class TestClaudeSessions:
    @pytest.mark.asyncio
    async def test_create_session(self):
        from sdk.backends.claude import ClaudeBackend

        b = ClaudeBackend(_make_auth())
        b.set_client_for_testing(AsyncMock())

        # create_session calls query() then iterates receive_response()
        result_msg = MagicMock()
        type(result_msg).__name__ = "ResultMessage"
        result_msg.session_id = "claude-sess-1"

        async def mock_receive():
            yield result_msg

        b.client.receive_response = mock_receive
        b.client.query = AsyncMock()

        ref = await b.create_session()
        assert ref.session_id == "claude-sess-1"
        assert ref.backend == Backend.CLAUDE

    @pytest.mark.asyncio
    async def test_list_sessions(self):
        from sdk.backends.claude import ClaudeBackend

        b = ClaudeBackend(_make_auth())
        b.set_client_for_testing(AsyncMock())

        # First create a session so there's something to list
        result_msg = MagicMock()
        type(result_msg).__name__ = "ResultMessage"
        result_msg.session_id = "s1"

        async def mock_receive():
            yield result_msg

        b.client.receive_response = mock_receive
        b.client.query = AsyncMock()

        await b.create_session()

        refs = await b.list_sessions()
        assert len(refs) == 1

    @pytest.mark.asyncio
    async def test_delete_session(self):
        from sdk.backends.claude import ClaudeBackend
        from sdk.internal.types import SessionRef

        b = ClaudeBackend(_make_auth())
        b.set_client_for_testing(AsyncMock())

        # delete_session just removes from session store, doesn't call client
        # First add a session to the store
        ref = SessionRef(session_id="s1", backend=Backend.CLAUDE)
        b.session_store.add(ref)

        await b.delete_session(ref)
        # After deletion, listing should be empty
        refs = await b.list_sessions()
        assert len(refs) == 0


class TestClaudeTools:
    def test_register_tool(self):
        from sdk.backends.claude import ClaudeBackend
        from sdk.internal.types import ToolSpec

        b = ClaudeBackend(_make_auth())
        spec = ToolSpec(
            name="t1", description="test tool", parameters={}, handler=lambda: None
        )
        b.register_tool(spec)
        assert len(b.tools) == 1

    def test_register_hook(self):
        from sdk.backends.claude import ClaudeBackend

        b = ClaudeBackend(_make_auth())
        cb = MagicMock()
        b.register_hook(HookPoint.STOP, cb)
        assert cb in b.hooks[HookPoint.STOP]


class TestClaudeInternals:
    def test_ensure_client_raises(self):
        from sdk.backends.claude import ClaudeBackend

        b = ClaudeBackend(_make_auth())
        with pytest.raises(RuntimeError, match="not started"):
            b.ensure_client_started()

    def test_to_message_empty(self):
        from sdk.backends.claude import ClaudeBackend

        b = ClaudeBackend(_make_auth())
        msg = b.to_message([])
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

        msg = b.to_message([assistant_msg])
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

        msg = b.to_message([assistant_msg])
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

        msg = b.to_message([assistant_msg])
        assert msg.content[0].kind == "tool_use"
        assert msg.content[0].tool_name == "my_tool"

    @pytest.mark.asyncio
    async def test_fork_session(self):
        from sdk.backends.claude import ClaudeBackend
        from sdk.internal.types import SessionRef

        b = ClaudeBackend(_make_auth())
        old_client = AsyncMock()
        b.set_client_for_testing(old_client)
        ref = SessionRef(session_id="sess-123", backend=Backend.CLAUDE)

        new_client = AsyncMock()
        with (
            patch("claude_agent_sdk.ClaudeSDKClient", return_value=new_client),
            patch("claude_agent_sdk.ClaudeAgentOptions"),
        ):
            out = await b.fork_session(ref)
            assert out is ref
            old_client.disconnect.assert_awaited_once()
            new_client.connect.assert_awaited_once()


class TestClaudeStream:
    @pytest.mark.asyncio
    async def test_stream_tool_choice_none_maps_disallowed_tools(self):
        from sdk.backends.claude import ClaudeBackend

        b = ClaudeBackend(_make_auth())
        mock_client = AsyncMock()
        b.set_client_for_testing(mock_client)
        b.register_tool(
            ToolSpec(
                name="search",
                description="search tool",
                parameters={"type": "object"},
                handler=_tool_handler,
            )
        )

        async def mock_receive():
            yield MagicMock()

        async def _fake_adapter(_source: Any) -> AsyncIterator[StreamChunk]:
            yield StreamChunk(kind=ChunkKind.MESSAGE_START)
            yield StreamChunk(kind=ChunkKind.DONE)

        mock_client.receive_response = mock_receive
        mock_client.query = AsyncMock()

        with patch("sdk.backends.claude.ClaudeIteratorAdapter", side_effect=_fake_adapter):
            chunks: list[StreamChunk] = []
            async for c in b.stream("ping", tool_choice=ToolChoice.none()):
                chunks.append(c)

        assert len(chunks) == 2
        mock_client.query.assert_awaited_once_with(
            "ping",
            disallowed_tools=["mcp__obscura_tools__search"],
        )
