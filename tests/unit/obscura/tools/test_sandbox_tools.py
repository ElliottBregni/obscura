"""Unit tests for Sandbox tools:
create_tool, call_dynamic_tool, list_dynamic_tools, code_sandbox."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import obscura.tools.system._sandbox as _sandbox_mod
from obscura.tools.system._sandbox import Sandbox

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_dynamic_tools() -> object:
    """Reset session-scoped dynamic tool registry between tests."""
    Sandbox.dynamic_tools.clear()
    yield
    Sandbox.dynamic_tools.clear()


@pytest.fixture(autouse=True)
def _full_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow path operations for code_sandbox save_as tests."""
    monkeypatch.setenv("OBSCURA_UNSAFE_FULL_ACCESS", "1")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_proc(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock()
    proc.kill = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# create_tool
# ---------------------------------------------------------------------------


async def test_create_tool_happy_path() -> None:
    result = json.loads(
        await Sandbox.create_tool(
            name="double_it",
            description="Doubles a number.",
            code='return json.dumps({"ok": True, "result": kwargs["x"] * 2})',
            parameters={"type": "object", "properties": {"x": {"type": "integer"}}},
        )
    )

    assert result["ok"] is True
    assert result["name"] == "double_it"
    assert "double_it" in Sandbox.dynamic_tools


async def test_create_tool_syntax_error_returns_error() -> None:
    result = json.loads(
        await Sandbox.create_tool(
            name="broken_tool",
            description="Has a syntax error.",
            code="def oops(: this is invalid python !!!",
        )
    )

    assert result["ok"] is False
    assert "syntax_error" in result.get("error", "")
    assert "broken_tool" not in Sandbox.dynamic_tools


async def test_create_tool_sanitises_name() -> None:
    """Non-alphanumeric chars in name become underscores."""
    result = json.loads(
        await Sandbox.create_tool(
            name="My Tool 2.0!",
            description="desc",
            code='return json.dumps({"ok": True})',
        )
    )

    assert result["ok"] is True
    # "My Tool 2.0!" → "my_tool_2_0_"
    assert result["name"].startswith("my_tool")


async def test_create_tool_name_conflict_with_builtin_returns_error() -> None:
    """Cannot shadow a built-in system tool."""
    result = json.loads(
        await Sandbox.create_tool(
            name="run_shell",
            description="shadow builtin",
            code='return json.dumps({"ok": True})',
        )
    )

    assert result["ok"] is False
    assert "conflicts" in result.get(
        "error", ""
    ).lower() or "name_conflicts" in result.get("error", "")


# ---------------------------------------------------------------------------
# call_dynamic_tool
# ---------------------------------------------------------------------------


async def test_call_dynamic_tool_invokes_handler() -> None:
    # First create a tool
    await Sandbox.create_tool(
        name="adder",
        description="Adds two numbers.",
        code='return json.dumps({"ok": True, "sum": kwargs["a"] + kwargs["b"]})',
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        },
    )

    result = json.loads(
        await Sandbox.call_dynamic_tool(name="adder", args={"a": 3, "b": 4})
    )

    assert result["ok"] is True
    assert result["sum"] == 7


async def test_call_dynamic_tool_not_found_returns_error() -> None:
    result = json.loads(
        await Sandbox.call_dynamic_tool(name="no_such_dynamic_tool_xyz")
    )

    assert result["ok"] is False
    assert "not_found" in result.get("error", "")


async def test_call_dynamic_tool_handler_exception_returns_error() -> None:
    await Sandbox.create_tool(
        name="exploding_tool",
        description="Always raises.",
        code='raise RuntimeError("boom")',
    )

    result = json.loads(await Sandbox.call_dynamic_tool(name="exploding_tool", args={}))

    assert result["ok"] is False
    assert "dynamic_tool_error" in result.get("error", "")


# ---------------------------------------------------------------------------
# list_dynamic_tools
# ---------------------------------------------------------------------------


async def test_list_dynamic_tools_empty() -> None:
    result = json.loads(await Sandbox.list_dynamic_tools())

    assert result["ok"] is True
    assert result["count"] == 0
    assert result["tools"] == []


async def test_list_dynamic_tools_after_create() -> None:
    await Sandbox.create_tool(
        name="tool_alpha",
        description="Alpha.",
        code='return json.dumps({"ok": True})',
    )
    await Sandbox.create_tool(
        name="tool_beta",
        description="Beta.",
        code='return json.dumps({"ok": True})',
    )

    result = json.loads(await Sandbox.list_dynamic_tools())

    assert result["ok"] is True
    assert result["count"] == 2
    names = {t["name"] for t in result["tools"]}
    assert {"tool_alpha", "tool_beta"} == names


# ---------------------------------------------------------------------------
# code_sandbox
# ---------------------------------------------------------------------------


async def test_code_sandbox_python_success() -> None:
    fake = _fake_proc(stdout=b"hello\n", returncode=0)
    with patch.object(
        _sandbox_mod.asyncio,
        "create_subprocess_exec",
        new=AsyncMock(return_value=fake),
    ):
        result = json.loads(
            await Sandbox.code_sandbox(language="python", code='print("hello")')
        )

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert "hello" in result["stdout"]
    assert result["language"] in ("python", "python3")


async def test_code_sandbox_nonzero_exit_returns_ok_false() -> None:
    fake = _fake_proc(stderr=b"NameError: x\n", returncode=1)
    with patch.object(
        _sandbox_mod.asyncio,
        "create_subprocess_exec",
        new=AsyncMock(return_value=fake),
    ):
        result = json.loads(
            await Sandbox.code_sandbox(language="python", code="print(x)")
        )

    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert "NameError" in result["stderr"]


async def test_code_sandbox_unsupported_language() -> None:
    result = json.loads(
        await Sandbox.code_sandbox(language="cobol", code="DISPLAY 'HELLO'.")
    )

    assert result["ok"] is False
    assert "unsupported_language" in result.get("error", "")


async def test_code_sandbox_bash_runs_shell() -> None:
    fake = _fake_proc(stdout=b"done\n", returncode=0)
    with patch.object(
        _sandbox_mod.asyncio,
        "create_subprocess_exec",
        new=AsyncMock(return_value=fake),
    ):
        result = json.loads(
            await Sandbox.code_sandbox(language="bash", code="echo done")
        )

    assert result["ok"] is True
    assert result["language"] == "bash"
    assert "done" in result["stdout"]


async def test_code_sandbox_timeout_returns_error() -> None:
    async def _slow_communicate(**_: object) -> tuple[bytes, bytes]:
        await asyncio.sleep(9999)
        return b"", b""

    proc = MagicMock()
    proc.returncode = -1
    proc.communicate = _slow_communicate
    proc.kill = MagicMock()
    proc.wait = AsyncMock()

    with patch.object(
        _sandbox_mod.asyncio,
        "create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        result = json.loads(
            await Sandbox.code_sandbox(
                language="python",
                code="import time; time.sleep(9999)",
                timeout_seconds=0.01,
            )
        )

    assert result["ok"] is False
    assert result.get("error") == "timeout"
