"""Tests for obscura.cli.widgets — TUI confirmation/question widgets."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from obscura.cli.widgets import (
    AttentionWidgetRequest,
    DetectedQuestion,
    ModelQuestionRequest,
    ToolConfirmRequest,
    WidgetResult,
    _format_arg_value,
    confirm_attention,
    confirm_tool,
    ask_model_question,
    detect_question_choices,
)


# ---------------------------------------------------------------------------
# _format_arg_value unit tests
# ---------------------------------------------------------------------------


class TestFormatArgValue:
    def test_short_string(self) -> None:
        result = _format_arg_value("hello")
        assert result == "hello"

    def test_dict_json(self) -> None:
        result = _format_arg_value({"key": "value"})
        assert '"key"' in result
        assert '"value"' in result

    def test_long_string_truncates(self) -> None:
        long_str = "x" * 600
        result = _format_arg_value(long_str)
        assert "600 chars total" in result
        assert len(result) < 600

    def test_multiline_truncates(self) -> None:
        lines = "\n".join(f"line {i}" for i in range(20))
        result = _format_arg_value(lines)
        assert "more lines" in result

    def test_list(self) -> None:
        result = _format_arg_value([1, 2, 3])
        assert "[" in result
        assert "1" in result


# ---------------------------------------------------------------------------
# confirm_tool tests (monkeypatched action bar)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_tool_allow() -> None:
    """Pressing 'y' returns allow."""
    with patch("obscura.cli.widgets._is_interactive", return_value=True), \
         patch("obscura.cli.widgets._render_tool_panel"), \
         patch("obscura.cli.widgets._run_action_bar", new_callable=AsyncMock, return_value="allow"):
        result = await confirm_tool(
            ToolConfirmRequest(tool_name="edit_file", tool_input={"path": "x.py"})
        )
    assert result.action == "allow"


@pytest.mark.asyncio
async def test_confirm_tool_deny() -> None:
    """Pressing 'n' returns deny."""
    with patch("obscura.cli.widgets._is_interactive", return_value=True), \
         patch("obscura.cli.widgets._render_tool_panel"), \
         patch("obscura.cli.widgets._run_action_bar", new_callable=AsyncMock, return_value="deny"):
        result = await confirm_tool(
            ToolConfirmRequest(tool_name="edit_file", tool_input={"path": "x.py"})
        )
    assert result.action == "deny"


@pytest.mark.asyncio
async def test_confirm_tool_always_allow() -> None:
    """Pressing 'a' returns always_allow."""
    with patch("obscura.cli.widgets._is_interactive", return_value=True), \
         patch("obscura.cli.widgets._render_tool_panel"), \
         patch("obscura.cli.widgets._run_action_bar", new_callable=AsyncMock, return_value="always_allow"):
        result = await confirm_tool(
            ToolConfirmRequest(tool_name="edit_file", tool_input={"path": "x.py"})
        )
    assert result.action == "always_allow"


@pytest.mark.asyncio
async def test_confirm_tool_nontty_fallback_yes() -> None:
    """Non-TTY falls back to text prompt, 'y' → allow."""
    with patch("obscura.cli.widgets._is_interactive", return_value=False), \
         patch("obscura.cli.widgets._fallback_confirm", new_callable=AsyncMock, return_value="y"):
        result = await confirm_tool(
            ToolConfirmRequest(tool_name="edit_file", tool_input={"path": "x.py"})
        )
    assert result.action == "allow"


@pytest.mark.asyncio
async def test_confirm_tool_nontty_fallback_always() -> None:
    """Non-TTY falls back to text prompt, 'always' → always_allow."""
    with patch("obscura.cli.widgets._is_interactive", return_value=False), \
         patch("obscura.cli.widgets._fallback_confirm", new_callable=AsyncMock, return_value="always"):
        result = await confirm_tool(
            ToolConfirmRequest(tool_name="edit_file", tool_input={"path": "x.py"})
        )
    assert result.action == "always_allow"


@pytest.mark.asyncio
async def test_confirm_tool_nontty_fallback_deny() -> None:
    """Non-TTY falls back to text prompt, 'n' → deny."""
    with patch("obscura.cli.widgets._is_interactive", return_value=False), \
         patch("obscura.cli.widgets._fallback_confirm", new_callable=AsyncMock, return_value="n"):
        result = await confirm_tool(
            ToolConfirmRequest(tool_name="edit_file", tool_input={"path": "x.py"})
        )
    assert result.action == "deny"


# ---------------------------------------------------------------------------
# confirm_attention tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_attention_custom_actions() -> None:
    """Selecting a custom action from attention request."""
    with patch("obscura.cli.widgets._is_interactive", return_value=True), \
         patch("obscura.cli.widgets._render_attention_panel"), \
         patch("obscura.cli.widgets._run_action_bar", new_callable=AsyncMock, return_value="source_a"):
        result = await confirm_attention(
            AttentionWidgetRequest(
                request_id="abc123",
                agent_name="researcher",
                message="Which source?",
                actions=("source_a", "source_b", "skip"),
            )
        )
    assert result.action == "source_a"


@pytest.mark.asyncio
async def test_confirm_attention_ok_default() -> None:
    """Single 'ok' action with default selection."""
    with patch("obscura.cli.widgets._is_interactive", return_value=True), \
         patch("obscura.cli.widgets._render_attention_panel"), \
         patch("obscura.cli.widgets._run_action_bar", new_callable=AsyncMock, return_value="ok"):
        result = await confirm_attention(
            AttentionWidgetRequest(
                request_id="abc123",
                agent_name="monitor",
                message="Task complete.",
            )
        )
    assert result.action == "ok"


@pytest.mark.asyncio
async def test_confirm_attention_nontty_fallback() -> None:
    """Non-TTY attention falls back to text prompt."""
    with patch("obscura.cli.widgets._is_interactive", return_value=False), \
         patch("obscura.cli.widgets._fallback_confirm", new_callable=AsyncMock, return_value="skip"):
        result = await confirm_attention(
            AttentionWidgetRequest(
                request_id="abc123",
                agent_name="researcher",
                message="Which source?",
                actions=("source_a", "source_b", "skip"),
            )
        )
    assert result.action == "skip"


# ---------------------------------------------------------------------------
# ask_model_question tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_model_question() -> None:
    """User types a response to a model question."""
    with patch("obscura.cli.widgets._is_interactive", return_value=True), \
         patch("obscura.cli.widgets._render_question_panel"), \
         patch("obscura.cli.widgets._run_text_input", new_callable=AsyncMock, return_value="use option B"):
        result = await ask_model_question(
            ModelQuestionRequest(question="Which approach?", source="assistant")
        )
    assert result.action == "respond"
    assert result.text == "use option B"


@pytest.mark.asyncio
async def test_ask_model_question_nontty() -> None:
    """Non-TTY model question falls back."""
    with patch("obscura.cli.widgets._is_interactive", return_value=False), \
         patch("obscura.cli.widgets._render_question_panel"), \
         patch("obscura.cli.widgets._fallback_confirm", new_callable=AsyncMock, return_value="option A"):
        result = await ask_model_question(
            ModelQuestionRequest(question="Which approach?")
        )
    assert result.action == "respond"
    assert result.text == "option A"


# ---------------------------------------------------------------------------
# Data class tests
# ---------------------------------------------------------------------------


class TestWidgetResult:
    def test_defaults(self) -> None:
        r = WidgetResult(action="allow")
        assert r.action == "allow"
        assert r.text == ""

    def test_with_text(self) -> None:
        r = WidgetResult(action="respond", text="hello")
        assert r.text == "hello"


class TestToolConfirmRequest:
    def test_fields(self) -> None:
        req = ToolConfirmRequest(
            tool_name="write_file",
            tool_input={"path": "/tmp/foo"},
            tool_use_id="abc",
        )
        assert req.tool_name == "write_file"
        assert req.tool_input == {"path": "/tmp/foo"}
        assert req.tool_use_id == "abc"


class TestAttentionWidgetRequest:
    def test_defaults(self) -> None:
        req = AttentionWidgetRequest(
            request_id="r1",
            agent_name="agent",
            message="hello",
        )
        assert req.priority == "normal"
        assert req.actions == ("ok",)
        assert req.context == {}

    def test_custom_actions(self) -> None:
        req = AttentionWidgetRequest(
            request_id="r1",
            agent_name="agent",
            message="pick one",
            actions=("a", "b", "c"),
        )
        assert len(req.actions) == 3


# ---------------------------------------------------------------------------
# detect_question_choices tests
# ---------------------------------------------------------------------------


class TestDetectQuestionChoices:
    def test_numbered_list_with_question(self) -> None:
        text = (
            "Which approach would you prefer?\n"
            "1. Use a REST API\n"
            "2. Use GraphQL\n"
            "3. Use gRPC\n"
        )
        detected = detect_question_choices(text)
        assert detected is not None
        assert len(detected.choices) == 3
        assert "Use a REST API" in detected.choices[0]

    def test_bulleted_list_with_question(self) -> None:
        text = (
            "Please choose one of the following:\n"
            "- Option A: fast but limited\n"
            "- Option B: slower but full-featured\n"
        )
        detected = detect_question_choices(text)
        assert detected is not None
        assert len(detected.choices) == 2

    def test_no_question_marker(self) -> None:
        """Should not trigger without a question-like sentence."""
        text = (
            "Here is what I found:\n"
            "1. File alpha.py\n"
            "2. File beta.py\n"
            "3. File gamma.py\n"
        )
        detected = detect_question_choices(text)
        assert detected is None

    def test_question_mark_trigger(self) -> None:
        """Should trigger if preamble ends with '?'."""
        text = (
            "How should we proceed?\n"
            "1. Refactor first\n"
            "2. Write tests first\n"
        )
        detected = detect_question_choices(text)
        assert detected is not None

    def test_too_few_items(self) -> None:
        text = "Which do you want?\n1. Only one option"
        detected = detect_question_choices(text)
        assert detected is None

    def test_empty_text(self) -> None:
        assert detect_question_choices("") is None

    def test_short_text(self) -> None:
        assert detect_question_choices("hi") is None

    def test_parenthesized_numbers(self) -> None:
        text = (
            "Would you like to:\n"
            "1) Keep the current design\n"
            "2) Switch to the new layout\n"
        )
        detected = detect_question_choices(text)
        assert detected is not None
        assert len(detected.choices) == 2


# ---------------------------------------------------------------------------
# ask_user tool tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_user_tool_with_choices() -> None:
    """ask_user tool with choices calls the callback and returns selection."""
    from obscura.tools.system import set_ask_user_callback, ask_user

    async def _mock_callback(
        question: str, choices: list[str], allow_custom: bool = False
    ) -> str:
        return choices[1]

    set_ask_user_callback(_mock_callback)
    try:
        import json
        result = await ask_user("Pick one", ["A", "B", "C"])
        parsed = json.loads(result)
        assert parsed["ok"] is True
        assert parsed["selected"] == "B"
    finally:
        set_ask_user_callback(None)


@pytest.mark.asyncio
async def test_ask_user_tool_no_callback() -> None:
    """ask_user tool without callback returns error."""
    from obscura.tools.system import set_ask_user_callback, ask_user

    set_ask_user_callback(None)
    import json
    result = await ask_user("Pick one", ["A", "B"])
    parsed = json.loads(result)
    assert parsed["ok"] is False
    assert parsed["error"] == "no_ui"
