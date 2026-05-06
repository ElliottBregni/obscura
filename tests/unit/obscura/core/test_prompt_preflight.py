"""Tests for obscura.core.prompt_preflight.

Covers the regex matchers (binary check, GitHub PR, GitLab MR), the
match-deduplication logic, and the dispatch path with a fake
``invoke_tool`` callback.
"""

from __future__ import annotations

import pytest

from obscura.core import prompt_preflight as pp


# ---------------------------------------------------------------------------
# find_matches — pattern coverage
# ---------------------------------------------------------------------------


def test_can_you_use_glab_matches_which() -> None:
    matches = pp.find_matches("yooo can you use the glab cli command?")
    assert len(matches) == 1
    m = matches[0]
    assert m.tool_name == "which_command"
    assert m.tool_input == {"command": "glab"}


def test_is_foo_installed_matches_which() -> None:
    matches = pp.find_matches("Is jq installed on this box?")
    assert len(matches) == 1
    assert matches[0].tool_name == "which_command"
    assert matches[0].tool_input == {"command": "jq"}


def test_stopword_does_not_trigger_binary_check() -> None:
    """`can you use it` must NOT trigger which-it."""
    assert pp.find_matches("can you use it for me") == []
    assert pp.find_matches("can you use that approach") == []


def test_unrelated_question_does_not_match() -> None:
    assert pp.find_matches("hello, what's the weather like") == []
    assert pp.find_matches("write me a poem") == []


def test_github_pr_url_matches() -> None:
    matches = pp.find_matches(
        "review https://github.com/anthropics/claude-code/pull/123 please",
    )
    assert len(matches) == 1
    m = matches[0]
    assert m.tool_name == "run_shell"
    cmd = m.tool_input["command"]
    assert isinstance(cmd, str)
    assert "gh pr view 123" in cmd
    assert "anthropics/claude-code" in cmd


def test_gitlab_mr_url_matches() -> None:
    matches = pp.find_matches(
        "can you help me review this code "
        "https://gitlab.com/freightverify-nextgen/FV-Platform-Main/-/merge_requests/17199",
    )
    # The "can you help me review this" doesn't match the binary pattern
    # because "review" is mid-phrase, not directly after "use/run". The
    # MR URL should match exactly once.
    mr_matches = [m for m in matches if "glab mr view" in str(m.tool_input.get("command", ""))]
    assert len(mr_matches) == 1
    cmd = mr_matches[0].tool_input["command"]
    assert isinstance(cmd, str)
    assert "glab mr view 17199" in cmd
    assert "freightverify-nextgen/FV-Platform-Main" in cmd


def test_self_hosted_gitlab_mr_uses_host_in_repo_arg() -> None:
    """Self-hosted GitLab needs ``host:path`` for ``glab -R``."""
    matches = pp.find_matches(
        "review https://gitlab.example.com/team/proj/-/merge_requests/42",
    )
    cmd = matches[0].tool_input["command"]
    assert isinstance(cmd, str)
    assert "glab mr view 42" in cmd
    assert "gitlab.example.com:team/proj" in cmd


def test_non_gitlab_host_with_merge_requests_path_does_not_match() -> None:
    """A random host with ``/-/merge_requests/`` shouldn't trigger glab."""
    matches = pp.find_matches(
        "see https://example.com/foo/-/merge_requests/1",
    )
    assert matches == []


def test_github_issue_url_matches() -> None:
    matches = pp.find_matches(
        "what's the deal with https://github.com/anthropics/claude-code/issues/456",
    )
    cmd = matches[0].tool_input["command"]
    assert isinstance(cmd, str)
    assert "gh issue view 456" in cmd
    assert "anthropics/claude-code" in cmd


def test_gitlab_issue_url_matches() -> None:
    matches = pp.find_matches(
        "context for https://gitlab.com/foo/bar/-/issues/7",
    )
    cmd = matches[0].tool_input["command"]
    assert isinstance(cmd, str)
    assert "glab issue view 7" in cmd
    assert "foo/bar" in cmd


# ---------------------------------------------------------------------------
# Python import question
# ---------------------------------------------------------------------------


def test_can_python_import_matches() -> None:
    matches = pp.find_matches("can python import requests?")
    py_matches = [m for m in matches if 'import_module("requests")' in str(m.tool_input.get("command", ""))]
    assert len(py_matches) == 1


def test_is_module_installed_matches() -> None:
    matches = pp.find_matches("is the numpy module installed?")
    py_matches = [m for m in matches if 'import_module("numpy")' in str(m.tool_input.get("command", ""))]
    assert len(py_matches) == 1


def test_does_module_exist_matches() -> None:
    matches = pp.find_matches("does the foo_bar module exist on this system?")
    py_matches = [m for m in matches if 'import_module("foo_bar")' in str(m.tool_input.get("command", ""))]
    assert len(py_matches) == 1


def test_python_import_rejects_shell_metachar_module_names() -> None:
    """Shell-injection-shaped module names must not produce a tool call."""
    matches = pp.find_matches("can python import foo; rm -rf /")
    py_matches = [m for m in matches if "rm -rf" in str(m.tool_input.get("command", ""))]
    assert py_matches == []


def test_unrelated_import_phrasing_does_not_match_python_rule() -> None:
    """`import` in a sentence without anchor words shouldn't fire."""
    matches = pp.find_matches("write me an import statement for csv")
    py_matches = [m for m in matches if "importlib" in str(m.tool_input.get("command", ""))]
    assert py_matches == []


# ---------------------------------------------------------------------------
# npm package question
# ---------------------------------------------------------------------------


def test_npm_view_matches() -> None:
    matches = pp.find_matches("what's the latest version of lodash on npm?")
    npm_matches = [m for m in matches if "npm view lodash" in str(m.tool_input.get("command", ""))]
    assert len(npm_matches) == 1


def test_is_pkg_on_npm_matches() -> None:
    matches = pp.find_matches("is react published on npm?")
    npm_matches = [m for m in matches if "npm view react" in str(m.tool_input.get("command", ""))]
    assert len(npm_matches) == 1


def test_npm_view_explicit_command_matches() -> None:
    matches = pp.find_matches("can you run npm view @types/node")
    npm_matches = [m for m in matches if "npm view @types/node" in str(m.tool_input.get("command", ""))]
    assert len(npm_matches) == 1


def test_npm_rejects_shell_metachar_in_pkg_name() -> None:
    matches = pp.find_matches("is foo;rm on npm?")
    npm_matches = [m for m in matches if ";rm" in str(m.tool_input.get("command", ""))]
    assert npm_matches == []


def test_multiple_matches_in_one_prompt() -> None:
    matches = pp.find_matches(
        "can you use glab? also review "
        "https://gitlab.com/foo/bar/-/merge_requests/1",
    )
    # which_command + glab mr view
    tool_names = sorted({m.tool_name for m in matches})
    assert tool_names == ["run_shell", "which_command"]


def test_dedup_same_tool_same_args() -> None:
    """Two rules producing identical (tool, args) only fire once."""

    fake_rule = pp.PreflightRule(
        name="dup",
        pattern=pp._BINARY_QUESTION_RE,
        build=lambda m: pp.PreflightMatch(
            tool_name="which_command",
            tool_input={"command": "glab"},
            reason="dup test",
        ),
    )
    matches = pp.find_matches(
        "can you use glab",
        rules=(pp.DEFAULT_RULES[0], fake_rule),
    )
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# run_preflight — async dispatch path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_preflight_dispatches_each_match() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def _fake(name: str, tool_input: dict[str, object]) -> str:
        calls.append((name, tool_input))
        return "stub-result"

    results = await pp.run_preflight(
        "can you use glab",
        invoke_tool=_fake,
    )
    assert len(results) == 1
    match, output = results[0]
    assert match.tool_name == "which_command"
    assert output == "stub-result"
    assert calls == [("which_command", {"command": "glab"})]


@pytest.mark.asyncio
async def test_run_preflight_swallows_tool_errors() -> None:
    """A failing tool must not block remaining matches or the user request."""

    async def _broken(_name: str, _input: dict[str, object]) -> str:
        msg = "boom"
        raise RuntimeError(msg)

    results = await pp.run_preflight(
        "can you use glab",
        invoke_tool=_broken,
    )
    assert results == []  # broken tool yielded no result, but didn't raise


@pytest.mark.asyncio
async def test_run_preflight_no_match_no_dispatch() -> None:
    calls = 0

    async def _fake(_name: str, _input: dict[str, object]) -> str:
        nonlocal calls
        calls += 1
        return ""

    results = await pp.run_preflight(
        "hello world",
        invoke_tool=_fake,
    )
    assert results == []
    assert calls == 0
