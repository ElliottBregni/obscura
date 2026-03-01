"""E2E-style integration test for A2A APER workflow using all system tools."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast, override
from unittest.mock import MagicMock, patch

import pytest

from demos.a2a.run_aper_10_agents import WorkflowA2AService, run_workflow
from obscura.agent.agent import BaseAgent
from obscura.tools.system import (
    append_text_file,
    call_dynamic_tool,
    clipboard_read,
    clipboard_write,
    code_sandbox,
    context_window_status,
    copilot_query,
    copy_path,
    create_tool,
    diff_files,
    discover_all_commands,
    download_file,
    edit_text_file,
    file_info,
    find_files,
    get_environment,
    get_system_info,
    get_system_tool_specs,
    git_branch,
    git_commit,
    git_diff,
    git_log,
    git_status,
    grep_files,
    http_request,
    json_query,
    list_directory,
    list_dynamic_tools,
    list_listening_ports,
    list_processes,
    list_system_tools,
    list_unix_capabilities,
    make_directory,
    manage_crontab,
    move_path,
    read_text_file,
    remove_path,
    run_command,
    run_npx,
    run_python,
    run_python3,
    run_shell,
    security_lookup,
    signal_process,
    task,
    tree_directory,
    web_fetch,
    web_search,
    which_command,
    write_text_file,
)
from obscura.core.types import AgentContext


class _SystemToolsAPERAgent(BaseAgent):
    def __init__(self, workspace: Path) -> None:
        super().__init__(client=cast(Any, MagicMock()), name="system-tools-aper")
        self._workspace = workspace

    @override
    async def analyze(self, ctx: AgentContext) -> None:
        ctx.analysis = {"input": str(ctx.input_data)}

    @override
    async def plan(self, ctx: AgentContext) -> None:
        ctx.plan = [spec.name for spec in get_system_tool_specs()]

    @override
    async def execute(self, ctx: AgentContext) -> None:
        temp_file = self._workspace / "notes.txt"
        temp_dir = self._workspace / "dir-a"
        removable_dir = self._workspace / "remove-me"
        tool_results: dict[str, dict[str, Any]] = {}

        async def _call(name: str, awaitable: Any) -> None:
            try:
                raw = await awaitable
                payload = json.loads(raw)
            except Exception as exc:  # pragma: no cover - defensive
                payload = {"ok": False, "error": str(exc)}
            tool_results[name] = payload

        # Execution tools
        await _call("run_python3", run_python3("print('aper-system-ok')"))
        await _call("run_python", run_python("print('aper-python-ok')"))
        await _call("run_npx", run_npx(["--version"]))
        await _call("run_command", run_command("echo", args=["aper-system-command"]))
        await _call("run_shell", run_shell("echo aper-shell-ok"))
        # Web tools (may fail without network, that's ok)
        await _call("web_fetch", web_fetch("http://localhost:0/test"))
        await _call("web_search", web_search("test query"))
        # Delegation
        await _call("task", task("test prompt"))
        # System discovery
        await _call("which_command", which_command("python3"))
        await _call("discover_all_commands", discover_all_commands(limit=120))
        # Filesystem — basic
        await _call("make_directory", make_directory(str(temp_dir)))
        await _call(
            "write_text_file",
            write_text_file(str(temp_file), "hello\n", overwrite=True),
        )
        await _call("append_text_file", append_text_file(str(temp_file), "world\n"))
        await _call("read_text_file", read_text_file(str(temp_file)))
        await _call("list_directory", list_directory(str(self._workspace)))
        # Filesystem — advanced
        await _call("grep_files", grep_files("hello", str(self._workspace)))
        await _call("find_files", find_files(str(self._workspace), pattern="*.txt"))
        await _call(
            "edit_text_file",
            edit_text_file(str(temp_file), "hello", "greetings"),
        )
        copy_dst = self._workspace / "notes_copy.txt"
        await _call("copy_path", copy_path(str(temp_file), str(copy_dst)))
        move_dst = self._workspace / "notes_moved.txt"
        await _call("move_path", move_path(str(copy_dst), str(move_dst)))
        await _call("file_info", file_info(str(temp_file)))
        await _call("tree_directory", tree_directory(str(self._workspace)))
        await _call("diff_files", diff_files(str(temp_file), str(move_dst)))
        # Git tools (may fail in temp dir, that's ok)
        await _call("git_status", git_status(cwd=str(self._workspace)))
        await _call("git_diff", git_diff(cwd=str(self._workspace)))
        await _call("git_log", git_log(cwd=str(self._workspace)))
        await _call("git_commit", git_commit("test", cwd=str(self._workspace)))
        await _call("git_branch", git_branch("list", cwd=str(self._workspace)))
        # Utility tools
        await _call("download_file", download_file("http://localhost:0/test", str(self._workspace / "dl.bin")))
        await _call("http_request", http_request("http://localhost:0/test"))
        await _call("clipboard_read", clipboard_read())
        await _call("clipboard_write", clipboard_write("test"))
        await _call(
            "json_query",
            json_query("name", data='{"name": "test"}'),
        )
        # Context window
        await _call("context_window_status", context_window_status())
        # Dynamic tools + sandbox
        await _call(
            "create_tool",
            create_tool("test_adder", "Add two numbers", {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}, "return json.dumps({'ok': True, 'result': kwargs.get('a', 0) + kwargs.get('b', 0)})"),
        )
        await _call("call_dynamic_tool", call_dynamic_tool("test_adder", {"a": 1, "b": 2}))
        await _call("list_dynamic_tools", list_dynamic_tools())
        await _call("code_sandbox", code_sandbox("python", "print('sandbox-ok')"))
        # Copilot GPT-5 Mini (may fail if copilot not installed, that's ok)
        await _call("copilot_query", copilot_query("say hello"))
        # System info
        await _call("get_environment", get_environment(prefix="PATH"))
        await _call("get_system_info", get_system_info())
        await _call("list_processes", list_processes())
        await _call("signal_process", signal_process(os.getpid(), signal="0"))
        await _call("list_listening_ports", list_listening_ports())
        await _call("security_lookup", security_lookup("logged_in_users"))
        await _call("manage_crontab", manage_crontab("list", marker="obscura"))
        await _call("list_unix_capabilities", list_unix_capabilities())
        await _call("list_system_tools", list_system_tools())
        await _call("make_directory.remove", make_directory(str(removable_dir)))
        await _call("remove_path", remove_path(str(removable_dir), recursive=True))

        # Normalize to registered names only
        tool_results.pop("make_directory.remove", None)
        ctx.results.append(tool_results)

    @override
    async def respond(self, ctx: AgentContext) -> None:
        latest = cast(dict[str, dict[str, Any]], ctx.results[-1] if ctx.results else {})
        ctx.response = json.dumps(
            {
                "ok": True,
                "executed_tools": sorted(latest.keys()),
                "tool_results": latest,
            }
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a2a_aper_workflow_executes_all_system_tools() -> None:
    expected = {spec.name for spec in get_system_tool_specs()}
    cached_response: str | None = None

    async def _patched_execute(self: WorkflowA2AService, task: Any, prompt: str) -> str:
        _ = self
        _ = task
        nonlocal cached_response
        if cached_response is None:
            with TemporaryDirectory() as tmpdir:
                agent = _SystemToolsAPERAgent(Path(tmpdir))
                cached_response = cast(str, await agent.run(prompt))
        return cached_response

    with patch.object(WorkflowA2AService, "_execute_agent", new=_patched_execute):
        outputs = await run_workflow("Run full APER system-tools e2e", model="copilot")

    assert len(outputs) == 10
    first_payload = json.loads(outputs[0][1])
    executed = set(cast(list[str], first_payload["executed_tools"]))
    assert executed == expected
    for _, text in outputs:
        assert text.strip() != ""
