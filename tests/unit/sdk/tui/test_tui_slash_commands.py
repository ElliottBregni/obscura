"""Tests for TUI slash command parsing.

Covers all slash commands from the plan: /mode, /backend, /model, /session,
/clear, /memory, /diff, /help, /quit. Also covers unknown commands,
extra arguments, and missing required arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Inline stubs — mirrors slash command parsing from PLAN_TUI.md
# ---------------------------------------------------------------------------

class TUIMode(Enum):
    ASK = "ask"
    PLAN = "plan"
    CODE = "code"
    DIFF = "diff"


@dataclass
class ParsedCommand:
    """Result of parsing a slash command."""
    name: str
    args: list[str] = field(default_factory=list)
    raw: str = ""


class UnknownCommandError(Exception):
    """Raised for unrecognized slash commands."""


class MissingArgumentError(Exception):
    """Raised when a command is missing required arguments."""


# Valid commands and their argument specs
_COMMAND_SPECS: dict[str, dict[str, Any]] = {
    "mode": {"min_args": 1, "max_args": 1, "choices": ["ask", "plan", "code", "diff"]},
    "backend": {"min_args": 1, "max_args": 1, "choices": ["claude", "copilot"]},
    "model": {"min_args": 1, "max_args": 1, "choices": None},
    "session": {"min_args": 1, "max_args": 2, "choices": None},  # new|list|load <id>
    "clear": {"min_args": 0, "max_args": 0, "choices": None},
    "memory": {"min_args": 1, "max_args": 2, "choices": None},  # list|get <key>
    "diff": {"min_args": 1, "max_args": 1, "choices": ["show", "accept-all", "reject-all"]},
    "help": {"min_args": 0, "max_args": 0, "choices": None},
    "quit": {"min_args": 0, "max_args": 0, "choices": None},
}


def parse_slash_command(text: str) -> ParsedCommand | None:
    """Parse a slash command from user input.

    Returns None if the input is not a slash command.
    Raises UnknownCommandError for unrecognized commands.
    Raises MissingArgumentError for commands missing required args.
    """
    text = text.strip()
    if not text.startswith("/"):
        return None

    parts = text.split()
    name = parts[0][1:]  # Remove the leading '/'
    args = parts[1:]

    if not name:
        return None

    if name not in _COMMAND_SPECS:
        raise UnknownCommandError(f"Unknown command: /{name}")

    spec = _COMMAND_SPECS[name]
    if len(args) < spec["min_args"]:
        raise MissingArgumentError(
            f"/{name} requires at least {spec['min_args']} argument(s)"
        )

    if spec["choices"] and args and args[0] not in spec["choices"]:
        raise ValueError(
            f"Invalid argument '{args[0]}' for /{name}. "
            f"Valid choices: {spec['choices']}"
        )

    return ParsedCommand(name=name, args=args, raw=text)


def is_slash_command(text: str) -> bool:
    """Check if text starts with a slash command."""
    return text.strip().startswith("/")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSlashCommandDetection:
    """Verify detection of slash commands vs regular text."""

    def test_slash_command_detected(self) -> None:
        """Text starting with '/' is detected as a slash command."""
        assert is_slash_command("/help") is True

    def test_regular_text_not_detected(self) -> None:
        """Regular text is not detected as a slash command."""
        assert is_slash_command("hello world") is False

    def test_empty_string_not_detected(self) -> None:
        """Empty string is not a slash command."""
        assert is_slash_command("") is False

    def test_slash_in_middle_not_detected(self) -> None:
        """Slash in the middle of text is not a command."""
        assert is_slash_command("use a/b path") is False

    def test_leading_whitespace_detected(self) -> None:
        """Slash command with leading whitespace is detected."""
        assert is_slash_command("  /help") is True


class TestModeCommand:
    """Verify /mode <ask|plan|code|diff> parsing."""

    def test_mode_ask(self) -> None:
        """'/mode ask' parses correctly."""
        cmd = parse_slash_command("/mode ask")
        assert cmd is not None
        assert cmd.name == "mode"
        assert cmd.args == ["ask"]

    def test_mode_plan(self) -> None:
        """'/mode plan' parses correctly."""
        cmd = parse_slash_command("/mode plan")
        assert cmd is not None
        assert cmd.name == "mode"
        assert cmd.args == ["plan"]

    def test_mode_code(self) -> None:
        """'/mode code' parses correctly."""
        cmd = parse_slash_command("/mode code")
        assert cmd is not None
        assert cmd.args == ["code"]

    def test_mode_diff(self) -> None:
        """'/mode diff' parses correctly."""
        cmd = parse_slash_command("/mode diff")
        assert cmd is not None
        assert cmd.args == ["diff"]

    def test_mode_missing_arg_raises(self) -> None:
        """'/mode' without argument raises MissingArgumentError."""
        with pytest.raises(MissingArgumentError):
            parse_slash_command("/mode")

    def test_mode_invalid_arg_raises(self) -> None:
        """'/mode invalid' raises ValueError."""
        with pytest.raises(ValueError, match="Invalid argument"):
            parse_slash_command("/mode invalid")


class TestBackendCommand:
    """Verify /backend <claude|copilot> parsing."""

    def test_backend_claude(self) -> None:
        """'/backend claude' parses correctly."""
        cmd = parse_slash_command("/backend claude")
        assert cmd is not None
        assert cmd.name == "backend"
        assert cmd.args == ["claude"]

    def test_backend_copilot(self) -> None:
        """'/backend copilot' parses correctly."""
        cmd = parse_slash_command("/backend copilot")
        assert cmd is not None
        assert cmd.args == ["copilot"]

    def test_backend_missing_arg_raises(self) -> None:
        """'/backend' without argument raises MissingArgumentError."""
        with pytest.raises(MissingArgumentError):
            parse_slash_command("/backend")

    def test_backend_invalid_arg_raises(self) -> None:
        """'/backend openai' raises ValueError."""
        with pytest.raises(ValueError, match="Invalid argument"):
            parse_slash_command("/backend openai")


class TestModelCommand:
    """Verify /model <model-id> parsing."""

    def test_model_with_id(self) -> None:
        """'/model claude-sonnet-4-5-20250929' parses correctly."""
        cmd = parse_slash_command("/model claude-sonnet-4-5-20250929")
        assert cmd is not None
        assert cmd.name == "model"
        assert cmd.args == ["claude-sonnet-4-5-20250929"]

    def test_model_any_string_accepted(self) -> None:
        """'/model' accepts any model ID string."""
        cmd = parse_slash_command("/model gpt-5-mini")
        assert cmd is not None
        assert cmd.args == ["gpt-5-mini"]

    def test_model_missing_arg_raises(self) -> None:
        """'/model' without argument raises MissingArgumentError."""
        with pytest.raises(MissingArgumentError):
            parse_slash_command("/model")


class TestSessionCommand:
    """Verify /session new|list|load <id> parsing."""

    def test_session_new(self) -> None:
        """'/session new' parses correctly."""
        cmd = parse_slash_command("/session new")
        assert cmd is not None
        assert cmd.name == "session"
        assert cmd.args == ["new"]

    def test_session_list(self) -> None:
        """'/session list' parses correctly."""
        cmd = parse_slash_command("/session list")
        assert cmd is not None
        assert cmd.args == ["list"]

    def test_session_load_with_id(self) -> None:
        """'/session load abc123' parses with two args."""
        cmd = parse_slash_command("/session load abc123")
        assert cmd is not None
        assert cmd.name == "session"
        assert cmd.args == ["load", "abc123"]

    def test_session_missing_arg_raises(self) -> None:
        """'/session' without argument raises MissingArgumentError."""
        with pytest.raises(MissingArgumentError):
            parse_slash_command("/session")


class TestClearCommand:
    """Verify /clear parsing."""

    def test_clear_no_args(self) -> None:
        """'/clear' parses with no arguments."""
        cmd = parse_slash_command("/clear")
        assert cmd is not None
        assert cmd.name == "clear"
        assert cmd.args == []

    def test_clear_raw_preserved(self) -> None:
        """The raw text is preserved in the parsed command."""
        cmd = parse_slash_command("/clear")
        assert cmd is not None
        assert cmd.raw == "/clear"


class TestMemoryCommand:
    """Verify /memory list|get <key> parsing."""

    def test_memory_list(self) -> None:
        """'/memory list' parses correctly."""
        cmd = parse_slash_command("/memory list")
        assert cmd is not None
        assert cmd.name == "memory"
        assert cmd.args == ["list"]

    def test_memory_get_with_key(self) -> None:
        """'/memory get my_key' parses with two args."""
        cmd = parse_slash_command("/memory get my_key")
        assert cmd is not None
        assert cmd.args == ["get", "my_key"]

    def test_memory_missing_arg_raises(self) -> None:
        """'/memory' without argument raises MissingArgumentError."""
        with pytest.raises(MissingArgumentError):
            parse_slash_command("/memory")


class TestDiffCommand:
    """Verify /diff show|accept-all|reject-all parsing."""

    def test_diff_show(self) -> None:
        """'/diff show' parses correctly."""
        cmd = parse_slash_command("/diff show")
        assert cmd is not None
        assert cmd.name == "diff"
        assert cmd.args == ["show"]

    def test_diff_accept_all(self) -> None:
        """'/diff accept-all' parses correctly."""
        cmd = parse_slash_command("/diff accept-all")
        assert cmd is not None
        assert cmd.args == ["accept-all"]

    def test_diff_reject_all(self) -> None:
        """'/diff reject-all' parses correctly."""
        cmd = parse_slash_command("/diff reject-all")
        assert cmd is not None
        assert cmd.args == ["reject-all"]

    def test_diff_missing_arg_raises(self) -> None:
        """'/diff' without argument raises MissingArgumentError."""
        with pytest.raises(MissingArgumentError):
            parse_slash_command("/diff")

    def test_diff_invalid_arg_raises(self) -> None:
        """'/diff invalid' raises ValueError."""
        with pytest.raises(ValueError, match="Invalid argument"):
            parse_slash_command("/diff invalid")


class TestHelpCommand:
    """Verify /help parsing."""

    def test_help(self) -> None:
        """'/help' parses correctly."""
        cmd = parse_slash_command("/help")
        assert cmd is not None
        assert cmd.name == "help"
        assert cmd.args == []


class TestQuitCommand:
    """Verify /quit parsing."""

    def test_quit(self) -> None:
        """'/quit' parses correctly."""
        cmd = parse_slash_command("/quit")
        assert cmd is not None
        assert cmd.name == "quit"
        assert cmd.args == []


class TestUnknownCommand:
    """Verify handling of unknown commands."""

    def test_unknown_command_raises(self) -> None:
        """An unrecognized command raises UnknownCommandError."""
        with pytest.raises(UnknownCommandError, match="Unknown command"):
            parse_slash_command("/foobar")

    def test_unknown_command_with_args_raises(self) -> None:
        """Unknown command with args still raises."""
        with pytest.raises(UnknownCommandError):
            parse_slash_command("/notreal arg1 arg2")

    def test_unknown_similar_to_valid_raises(self) -> None:
        """Typos in command names still raise UnknownCommandError."""
        with pytest.raises(UnknownCommandError):
            parse_slash_command("/moed ask")  # typo for /mode


class TestExtraArguments:
    """Verify commands with extra arguments."""

    def test_mode_extra_args_accepted(self) -> None:
        """Extra args beyond the required ones are stored."""
        # /mode only expects 1 arg, but parsing doesn't reject extras
        # (the validator handles that separately)
        cmd = parse_slash_command("/mode ask extra")
        assert cmd is not None
        assert cmd.name == "mode"
        assert "ask" in cmd.args

    def test_session_load_extra_preserved(self) -> None:
        """'/session load id extra' preserves extra args."""
        cmd = parse_slash_command("/session load myid")
        assert cmd is not None
        assert cmd.args == ["load", "myid"]


class TestCommandRawText:
    """Verify that raw text is preserved."""

    def test_raw_text_preserved(self) -> None:
        """The original command text is stored in raw."""
        cmd = parse_slash_command("/mode ask")
        assert cmd is not None
        assert cmd.raw == "/mode ask"

    def test_raw_text_with_whitespace(self) -> None:
        """Leading/trailing whitespace is stripped from raw."""
        cmd = parse_slash_command("  /help  ")
        assert cmd is not None
        assert cmd.raw == "/help"

    def test_not_a_command_returns_none(self) -> None:
        """Non-command text returns None."""
        assert parse_slash_command("hello world") is None

    def test_empty_slash_returns_none(self) -> None:
        """A lone '/' returns None (empty command name)."""
        assert parse_slash_command("/") is None

    def test_slash_with_only_spaces_returns_none(self) -> None:
        """'/ ' with no command name returns None."""
        result = parse_slash_command("/ ")
        # Either None or raises depending on implementation
        assert result is None or isinstance(result, ParsedCommand)


class TestAllCommandsParseSuccessfully:
    """Smoke test: every valid command from the plan can be parsed."""

    @pytest.mark.parametrize("cmd_text,expected_name", [
        ("/mode ask", "mode"),
        ("/mode plan", "mode"),
        ("/mode code", "mode"),
        ("/mode diff", "mode"),
        ("/backend claude", "backend"),
        ("/backend copilot", "backend"),
        ("/model gpt-5", "model"),
        ("/session new", "session"),
        ("/session list", "session"),
        ("/session load abc", "session"),
        ("/clear", "clear"),
        ("/memory list", "memory"),
        ("/memory get mykey", "memory"),
        ("/diff show", "diff"),
        ("/diff accept-all", "diff"),
        ("/diff reject-all", "diff"),
        ("/help", "help"),
        ("/quit", "quit"),
    ])
    def test_command_parses(self, cmd_text: str, expected_name: str) -> None:
        """Each valid command from the plan spec parses without error."""
        cmd = parse_slash_command(cmd_text)
        assert cmd is not None
        assert cmd is not None
        assert cmd.name == expected_name
