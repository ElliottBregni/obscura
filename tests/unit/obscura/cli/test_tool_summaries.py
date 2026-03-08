"""Tests for obscura.cli.tool_summaries."""

from __future__ import annotations

from obscura.cli.tool_summaries import summarize_tool_call


def test_read_text_file() -> None:
    assert summarize_tool_call("read_text_file", {"path": "/foo/bar/render.py"}) == "Reading render.py"


def test_read_text_file_no_path() -> None:
    assert summarize_tool_call("read_text_file", {}) == "Reading file"


def test_write_text_file() -> None:
    assert summarize_tool_call("write_text_file", {"path": "/tmp/out.py", "text": "..."}) == "Writing out.py"


def test_edit_text_file() -> None:
    assert summarize_tool_call("edit_text_file", {"path": "src/main.py", "old_text": "x", "new_text": "y"}) == "Editing main.py"


def test_append_text_file() -> None:
    assert summarize_tool_call("append_text_file", {"path": "/tmp/log.txt"}) == "Appending to log.txt"


def test_grep_files() -> None:
    assert summarize_tool_call("grep_files", {"pattern": "StreamRenderer", "path": "."}) == "Searching for 'StreamRenderer'"


def test_grep_files_no_pattern() -> None:
    assert summarize_tool_call("grep_files", {}) == "Searching files"


def test_find_files() -> None:
    assert summarize_tool_call("find_files", {"pattern": "*.py"}) == "Finding '*.py'"


def test_run_shell() -> None:
    assert summarize_tool_call("run_shell", {"script": "ls -la"}) == "$ ls -la"


def test_run_shell_long_cmd() -> None:
    long_cmd = "x" * 100
    result = summarize_tool_call("run_shell", {"script": long_cmd})
    assert result.startswith("$ ")
    assert len(result) <= 65  # "$ " + truncated


def test_run_command() -> None:
    result = summarize_tool_call("run_command", {"command": "git", "args": ["status"]})
    assert result == "$ git status"


def test_run_npx() -> None:
    result = summarize_tool_call("run_npx", {"command": "tsc", "args": ["--noEmit"]})
    assert "npx tsc" in result


def test_run_python() -> None:
    result = summarize_tool_call("run_python", {"code": "print('hello')\nx = 1"})
    assert "print('hello')" in result


def test_web_fetch() -> None:
    result = summarize_tool_call("web_fetch", {"url": "https://example.com"})
    assert result == "Fetching https://example.com"


def test_web_search() -> None:
    assert summarize_tool_call("web_search", {"query": "python asyncio"}) == "Searching web for 'python asyncio'"


def test_git_status() -> None:
    assert summarize_tool_call("git_status", {}) == "git status"


def test_git_diff() -> None:
    assert summarize_tool_call("git_diff", {}) == "git diff"


def test_git_diff_with_ref() -> None:
    assert summarize_tool_call("git_diff", {"ref": "HEAD~3"}) == "git diff HEAD~3"


def test_git_commit() -> None:
    assert summarize_tool_call("git_commit", {"message": "Fix bug in auth"}) == 'git commit -m "Fix bug in auth"'


def test_git_branch() -> None:
    assert summarize_tool_call("git_branch", {}) == "git branch"


def test_list_directory() -> None:
    result = summarize_tool_call("list_directory", {"path": "/tmp/mydir"})
    assert "mydir" in result


def test_tree_directory() -> None:
    result = summarize_tool_call("tree_directory", {"path": "/home/user/project"})
    assert "project" in result


def test_task_delegate() -> None:
    result = summarize_tool_call("task", {"prompt": "Analyze the auth module"})
    assert result == "Delegating: Analyze the auth module"


def test_http_request() -> None:
    result = summarize_tool_call("http_request", {"method": "post", "url": "https://api.example.com/v1"})
    assert result == "POST https://api.example.com/v1"


def test_clipboard_read() -> None:
    assert summarize_tool_call("clipboard_read", {}) == "Reading clipboard"


def test_context_window_status() -> None:
    assert summarize_tool_call("context_window_status", {}) == "Checking context window"


def test_todo_write() -> None:
    assert summarize_tool_call("todo_write", {}) == "Updating todos"


def test_unknown_tool_fallback() -> None:
    result = summarize_tool_call("unknown_tool", {"x": "1", "y": "2"})
    assert "unknown_tool" in result
    assert "x=1" in result


def test_unknown_tool_no_args() -> None:
    assert summarize_tool_call("unknown_tool", {}) == "unknown_tool"


def test_fallback_truncates_long_values() -> None:
    result = summarize_tool_call("unknown_tool", {"data": "a" * 100})
    assert "..." in result
    assert len(result) < 100


def test_code_sandbox() -> None:
    result = summarize_tool_call("code_sandbox", {"language": "javascript"})
    assert result == "Running javascript sandbox"


def test_ask_user() -> None:
    result = summarize_tool_call("ask_user", {"question": "Which approach?"})
    assert result == "Asking: Which approach?"


def test_signal_process() -> None:
    result = summarize_tool_call("signal_process", {"pid": 1234, "signal": "SIGKILL"})
    assert "1234" in result
    assert "SIGKILL" in result
