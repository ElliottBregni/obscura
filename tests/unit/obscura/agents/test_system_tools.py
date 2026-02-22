"""Tests for sdk.agent.system_tools."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    manage_crontab,
    make_directory,
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


class TestSystemToolSpecs:
    def test_returns_expected_specs(self) -> None:
        specs = get_system_tool_specs()
        names = {spec.name for spec in specs}
        assert "run_python3" in names
        assert "run_npx" in names
        assert "run_command" in names
        assert "run_shell" in names
        assert "list_directory" in names
        assert "read_text_file" in names
        assert "write_text_file" in names
        assert "append_text_file" in names
        assert "make_directory" in names
        assert "remove_path" in names
        assert "get_environment" in names
        assert "get_system_info" in names
        assert "list_system_tools" in names
        assert "which_command" in names
        assert "discover_all_commands" in names
        assert "list_processes" in names
        assert "signal_process" in names
        assert "list_listening_ports" in names
        assert "security_lookup" in names
        assert "manage_crontab" in names
        assert "list_unix_capabilities" in names


class TestRunPython3:
    @pytest.mark.asyncio
    async def test_executes_python_code(self) -> None:
        output = await run_python3("print('ok')")
        payload = json.loads(output)
        assert payload["ok"] is True
        assert payload["exit_code"] == 0
        assert "ok" in payload["stdout"]


class TestRunNpx:
    @pytest.mark.asyncio
    async def test_timeout_returns_error_payload(self) -> None:
        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        with patch(
            "obscura.tools.system.asyncio.create_subprocess_exec", return_value=proc
        ):
            payload = json.loads(await run_npx(["--version"], timeout_seconds=0.01))
        assert payload["ok"] is False
        assert payload["error"] == "timeout"

    def test_npx_spec_requires_args(self) -> None:
        specs = {spec.name: spec for spec in get_system_tool_specs()}
        npx_schema = specs["run_npx"].parameters
        assert "args" in npx_schema.get("required", [])


class TestRunCommand:
    @pytest.mark.asyncio
    async def test_executes_command(self) -> None:
        output = await run_command("echo", args=["hello"])
        payload = json.loads(output)
        assert payload["ok"] is True
        assert payload["exit_code"] == 0
        assert "hello" in payload["stdout"]

    @pytest.mark.asyncio
    async def test_denied_command_returns_error(self) -> None:
        output = await run_command("rm", args=["-rf", "/tmp/nope"])
        payload = json.loads(output)
        assert payload["ok"] is False
        assert payload["error"] == "command_denied"

    @pytest.mark.asyncio
    async def test_allowlist_blocks_unknown_command(self) -> None:
        with patch.dict(
            os.environ,
            {"OBSCURA_SYSTEM_TOOLS_ALLOWED_COMMANDS": "echo,python3"},
            clear=False,
        ):
            output = await run_command("ls")
        payload = json.loads(output)
        assert payload["ok"] is False
        assert payload["error"] == "command_not_allowed"

    @pytest.mark.asyncio
    async def test_timeout_returns_error_payload(self) -> None:
        proc = AsyncMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        with patch(
            "obscura.tools.system.asyncio.create_subprocess_exec", return_value=proc
        ):
            payload = json.loads(
                await run_command("echo", args=["slow"], timeout_seconds=0.01)
            )
        assert payload["ok"] is False
        assert payload["error"] == "timeout"

    @pytest.mark.asyncio
    async def test_cwd_not_allowed_returns_error(self) -> None:
        with patch.dict(
            os.environ,
            {"OBSCURA_SYSTEM_TOOLS_BASE_DIR": "/tmp/base"},
            clear=False,
        ):
            output = await run_command("echo", args=["x"], cwd="/tmp/other")
        payload = json.loads(output)
        assert payload["ok"] is False
        assert payload["error"] == "cwd_not_allowed"

    @pytest.mark.asyncio
    async def test_empty_denied_env_disables_default_denylist(self) -> None:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        with (
            patch.dict(
                os.environ,
                {"OBSCURA_SYSTEM_TOOLS_DENIED_COMMANDS": ""},
                clear=False,
            ),
            patch(
                "obscura.tools.system.asyncio.create_subprocess_exec",
                return_value=proc,
            ),
        ):
            payload = json.loads(await run_command("rm", args=["--version"]))
        assert payload["ok"] is True

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        payload = json.loads(await run_command("definitely-no-such-command"))
        assert payload["ok"] is False
        assert payload["error"] == "command_not_found"

    @pytest.mark.asyncio
    async def test_unsafe_full_access_bypasses_default_denylist(self) -> None:
        with patch.dict(
            os.environ,
            {"OBSCURA_SYSTEM_TOOLS_UNSAFE_FULL_ACCESS": "true"},
            clear=False,
        ):
            payload = json.loads(await run_command("echo", args=["unsafe-ok"]))
        assert payload["ok"] is True
        assert "unsafe-ok" in payload["stdout"]


class TestRunShell:
    @pytest.mark.asyncio
    async def test_executes_shell_script(self) -> None:
        payload = json.loads(await run_shell("echo shell-ok"))
        assert payload["ok"] is True
        assert "shell-ok" in payload["stdout"]


class TestFilesystemTools:
    @pytest.mark.asyncio
    async def test_write_read_append_and_list(self) -> None:
        with TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            file_path = base / "a" / "notes.txt"

            write_payload = json.loads(
                await write_text_file(str(file_path), "line1\n", create_dirs=True)
            )
            assert write_payload["ok"] is True

            append_payload = json.loads(
                await append_text_file(str(file_path), "line2\n")
            )
            assert append_payload["ok"] is True

            read_payload = json.loads(await read_text_file(str(file_path)))
            assert read_payload["ok"] is True
            assert "line1" in read_payload["text"]
            assert "line2" in read_payload["text"]

            list_payload = json.loads(await list_directory(str(base / "a")))
            assert list_payload["ok"] is True
            assert any(
                entry["name"] == "notes.txt" for entry in list_payload["entries"]
            )

    @pytest.mark.asyncio
    async def test_make_and_remove_directory(self) -> None:
        with TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "to-remove"
            made = json.loads(await make_directory(str(target)))
            assert made["ok"] is True
            assert target.exists()

            removed_fail = json.loads(await remove_path(str(target), recursive=False))
            assert removed_fail["ok"] is False
            assert removed_fail["error"] == "directory_requires_recursive_true"

            removed_ok = json.loads(await remove_path(str(target), recursive=True))
            assert removed_ok["ok"] is True
            assert not target.exists()

    @pytest.mark.asyncio
    async def test_base_dir_restriction_applies_to_file_ops(self) -> None:
        with TemporaryDirectory() as allowed, TemporaryDirectory() as blocked:
            blocked_file = Path(blocked) / "x.txt"
            with patch.dict(
                os.environ,
                {"OBSCURA_SYSTEM_TOOLS_BASE_DIR": allowed},
                clear=False,
            ):
                payload = json.loads(
                    await write_text_file(str(blocked_file), "blocked")
                )
            assert payload["ok"] is False
            assert payload["error"] == "path_not_allowed"


class TestDiscoveryTools:
    @pytest.mark.asyncio
    async def test_which_command(self) -> None:
        payload = json.loads(await which_command("python3"))
        assert payload["ok"] is True
        assert payload["exists"] is True

    @pytest.mark.asyncio
    async def test_discover_all_commands(self) -> None:
        payload = json.loads(await discover_all_commands(limit=200))
        assert payload["ok"] is True
        assert payload["count"] > 0
        assert "commands" in payload

    @pytest.mark.asyncio
    async def test_discover_all_commands_prefix(self) -> None:
        payload = json.loads(await discover_all_commands(limit=200, prefix="py"))
        assert payload["ok"] is True
        assert all(cmd.startswith("py") for cmd in payload["commands"])

    @pytest.mark.asyncio
    async def test_get_environment_prefix(self) -> None:
        with patch.dict(
            os.environ,
            {"OBSCURA_TEST_A": "1", "OTHER": "x"},
            clear=False,
        ):
            payload = json.loads(
                await get_environment(prefix="OBSCURA_TEST_", include_values=True)
            )
        assert payload["ok"] is True
        assert payload["count"] >= 1
        assert "OBSCURA_TEST_A" in payload["variables"]
        assert "OTHER" not in payload["variables"]

    @pytest.mark.asyncio
    async def test_get_system_info(self) -> None:
        payload = json.loads(await get_system_info())
        assert payload["ok"] is True
        assert "platform" in payload["info"]
        assert "commands" in payload["info"]

    @pytest.mark.asyncio
    async def test_list_system_tools(self) -> None:
        payload = json.loads(await list_system_tools())
        assert payload["ok"] is True
        names = {item["name"] for item in payload["tools"]}
        assert "run_command" in names
        assert "write_text_file" in names

    @pytest.mark.asyncio
    async def test_list_unix_capabilities(self) -> None:
        payload = json.loads(await list_unix_capabilities())
        assert payload["ok"] is True
        assert isinstance(payload["tools_count"], int)
        assert "run_command" in payload["tools"]


class TestOpsAndSecurityTools:
    @pytest.mark.asyncio
    async def test_list_processes(self) -> None:
        payload = json.loads(await list_processes())
        assert payload["ok"] is True

    @pytest.mark.asyncio
    async def test_signal_process_rejects_bad_pid(self) -> None:
        payload = json.loads(await signal_process(999999, signal="TERM"))
        assert payload["ok"] is False

    @pytest.mark.asyncio
    async def test_list_listening_ports(self) -> None:
        payload = json.loads(await list_listening_ports())
        assert "ok" in payload

    @pytest.mark.asyncio
    async def test_security_lookup_logged_in_users(self) -> None:
        payload = json.loads(await security_lookup("logged_in_users"))
        assert "ok" in payload

    @pytest.mark.asyncio
    async def test_security_lookup_world_writable(self) -> None:
        with TemporaryDirectory() as tmpdir:
            payload = json.loads(
                await security_lookup("world_writable", path=tmpdir, max_results=10)
            )
        assert "ok" in payload

    @pytest.mark.asyncio
    async def test_manage_crontab_list_without_binary(self) -> None:
        with patch("obscura.tools.system.shutil.which", return_value=None):
            payload = json.loads(await manage_crontab("list"))
        assert payload["ok"] is False
        assert payload["error"] == "crontab_not_found"

    @pytest.mark.asyncio
    async def test_manage_crontab_add_requires_fields(self) -> None:
        with patch(
            "obscura.tools.system.shutil.which", return_value="/usr/bin/crontab"
        ):
            payload = json.loads(await manage_crontab("add", schedule="", command=""))
        assert payload["ok"] is False
        assert payload["error"] == "schedule_and_command_required"
