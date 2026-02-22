"""Integration tests for system tools on macOS hosts."""

from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from obscura.tools.system import (
    append_text_file,
    discover_all_commands,
    get_environment,
    get_system_info,
    list_directory,
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
    which_command,
    write_text_file,
)


def _has_npx() -> bool:
    if shutil.which("npx") is not None:
        return True
    # Mirrors sdk.agent.system_tools nvm fallback
    home = os.path.expanduser("~")
    return os.path.exists(f"{home}/.nvm/versions/node")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_python3_real_execution_on_macbook() -> None:
    if platform.system() != "Darwin":
        pytest.skip("macOS-only integration test")
    output = await run_python3("print('obscura-system-python-ok')")
    payload = json.loads(output)
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert "obscura-system-python-ok" in payload["stdout"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_npx_real_execution_on_macbook() -> None:
    if platform.system() != "Darwin":
        pytest.skip("macOS-only integration test")
    if not _has_npx():
        pytest.skip("npx not available on this machine")
    output = await run_npx(["--version"], timeout_seconds=30.0)
    payload = json.loads(output)
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert payload["stdout"].strip() != ""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_command_real_execution_on_macbook() -> None:
    if platform.system() != "Darwin":
        pytest.skip("macOS-only integration test")
    output = await run_command("echo", args=["obscura-system-command-ok"])
    payload = json.loads(output)
    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert "obscura-system-command-ok" in payload["stdout"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_shell_real_execution_on_macbook() -> None:
    if platform.system() != "Darwin":
        pytest.skip("macOS-only integration test")
    payload = json.loads(await run_shell("echo obscura-system-shell-ok"))
    assert payload["ok"] is True
    assert "obscura-system-shell-ok" in payload["stdout"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filesystem_tools_real_execution_on_macbook() -> None:
    if platform.system() != "Darwin":
        pytest.skip("macOS-only integration test")
    with TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        nested = base / "tools" / "dir"
        mk = json.loads(await make_directory(str(nested)))
        assert mk["ok"] is True

        file_path = nested / "real.txt"
        wr = json.loads(await write_text_file(str(file_path), "line-a\n"))
        assert wr["ok"] is True

        ap = json.loads(await append_text_file(str(file_path), "line-b\n"))
        assert ap["ok"] is True

        rd = json.loads(await read_text_file(str(file_path)))
        assert rd["ok"] is True
        assert "line-a" in rd["text"]
        assert "line-b" in rd["text"]

        ls = json.loads(await list_directory(str(nested)))
        assert ls["ok"] is True
        assert any(entry["name"] == "real.txt" for entry in ls["entries"])

        rm = json.loads(await remove_path(str(base / "tools"), recursive=True))
        assert rm["ok"] is True
        assert not (base / "tools").exists()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_discovery_tools_real_execution_on_macbook() -> None:
    if platform.system() != "Darwin":
        pytest.skip("macOS-only integration test")
    wh = json.loads(await which_command("python3"))
    assert wh["ok"] is True

    env = json.loads(await get_environment(prefix="PATH", include_values=False))
    assert env["ok"] is True

    info = json.loads(await get_system_info())
    assert info["ok"] is True
    assert info["info"]["system"] == "Darwin"

    tools = json.loads(await list_system_tools())
    assert tools["ok"] is True
    names = {item["name"] for item in tools["tools"]}
    assert "run_command" in names
    assert "write_text_file" in names

    discovered = json.loads(await discover_all_commands(limit=300))
    assert discovered["ok"] is True
    assert discovered["count"] > 0
    assert isinstance(discovered["commands"], list)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ops_security_tools_real_execution_on_macbook() -> None:
    if platform.system() != "Darwin":
        pytest.skip("macOS-only integration test")

    procs = json.loads(await list_processes())
    assert procs["ok"] is True

    sec = json.loads(await security_lookup("logged_in_users"))
    assert "ok" in sec

    caps = json.loads(await list_unix_capabilities())
    assert caps["ok"] is True
    assert "run_command" in caps["tools"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_manage_crontab_list_only_real_execution_on_macbook() -> None:
    if platform.system() != "Darwin":
        pytest.skip("macOS-only integration test")
    payload = json.loads(await manage_crontab("list", marker="obscura"))
    assert "ok" in payload
