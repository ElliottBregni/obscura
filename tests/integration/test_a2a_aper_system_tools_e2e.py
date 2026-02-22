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
    discover_all_commands,
    get_environment,
    get_system_info,
    get_system_tool_specs,
    list_directory,
    list_listening_ports,
    list_processes,
    list_system_tools,
    list_unix_capabilities,
    make_directory,
    manage_crontab,
    read_text_file,
    remove_path,
    run_command,
    run_npx,
    run_python3,
    run_shell,
    security_lookup,
    signal_process,
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

        await _call("run_python3", run_python3("print('aper-system-ok')"))
        await _call("run_npx", run_npx(["--version"]))
        await _call("run_command", run_command("echo", args=["aper-system-command"]))
        await _call("run_shell", run_shell("echo aper-shell-ok"))
        await _call("which_command", which_command("python3"))
        await _call("discover_all_commands", discover_all_commands(limit=120))
        await _call("make_directory", make_directory(str(temp_dir)))
        await _call(
            "write_text_file",
            write_text_file(str(temp_file), "hello\n", overwrite=True),
        )
        await _call("append_text_file", append_text_file(str(temp_file), "world\n"))
        await _call("read_text_file", read_text_file(str(temp_file)))
        await _call("list_directory", list_directory(str(self._workspace)))
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

    async def _patched_execute(
        self: WorkflowA2AService, task: Any, prompt: str
    ) -> str:
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
