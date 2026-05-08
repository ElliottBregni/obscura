"""Unit tests for obscura.core.tool_bridge.call_tool_handler pipeline."""
# pyright: reportPrivateUsage=false, reportUnknownLambdaType=false

from __future__ import annotations

import pytest

from obscura.core.tool_bridge import call_tool_handler
from obscura.core.types import SideEffects, ToolSpec

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(
    name: str,
    handler: object,
    params: dict[str, object] | None = None,
    required: list[str] | None = None,
    side_effects: SideEffects = SideEffects.NONE,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="test",
        parameters={
            "type": "object",
            "properties": params or {},
            "required": required or [],
        },
        handler=handler,  # type: ignore[arg-type]
        side_effects=side_effects,
    )


# ---------------------------------------------------------------------------
# Step 5: handler invocation
# ---------------------------------------------------------------------------


async def test_sync_handler_called_with_args() -> None:
    result = await call_tool_handler(
        _spec(
            "t", lambda x: f"got:{x}", params={"x": {"type": "string"}}, required=["x"]
        ),
        {"x": "hi"},
    )
    assert result == "got:hi"


async def test_async_handler_awaited() -> None:
    async def handler(x: str) -> str:
        return f"async:{x}"

    result = await call_tool_handler(
        _spec("t", handler, params={"x": {"type": "string"}}, required=["x"]),
        {"x": "bye"},
    )
    assert result == "async:bye"


# ---------------------------------------------------------------------------
# Step 4: type coercion
# ---------------------------------------------------------------------------


async def test_coercion_string_to_int() -> None:
    received: dict[str, object] = {}

    def handler(n: int) -> str:
        received["n"] = n
        return "ok"

    await call_tool_handler(
        _spec("t", handler, params={"n": {"type": "integer"}}, required=["n"]),
        {"n": "42"},
    )
    assert received["n"] == 42
    assert isinstance(received["n"], int)


async def test_coercion_string_true_to_bool() -> None:
    received: dict[str, object] = {}

    def handler(flag: bool) -> str:
        received["flag"] = flag
        return "ok"

    await call_tool_handler(
        _spec("t", handler, params={"flag": {"type": "boolean"}}, required=["flag"]),
        {"flag": "true"},
    )
    assert received["flag"] is True


async def test_coercion_string_false_to_bool() -> None:
    received: dict[str, object] = {}

    def handler(flag: bool) -> str:
        received["flag"] = flag
        return "ok"

    await call_tool_handler(
        _spec("t", handler, params={"flag": {"type": "boolean"}}, required=["flag"]),
        {"flag": "false"},
    )
    assert received["flag"] is False


async def test_coercion_string_to_float() -> None:
    received: dict[str, object] = {}

    def handler(v: float) -> str:
        received["v"] = v
        return "ok"

    await call_tool_handler(
        _spec("t", handler, params={"v": {"type": "number"}}, required=["v"]),
        {"v": "3.14"},
    )
    assert isinstance(received["v"], float)
    assert abs(float(received["v"]) - 3.14) < 1e-9


# ---------------------------------------------------------------------------
# Step 3: required-field pre-check
# ---------------------------------------------------------------------------


async def test_missing_required_field_raises_type_error() -> None:
    with pytest.raises(TypeError, match="missing"):
        await call_tool_handler(
            _spec("t", lambda x: x, params={"x": {"type": "string"}}, required=["x"]),
            {},
        )


# ---------------------------------------------------------------------------
# Step 2: parameter aliases
# ---------------------------------------------------------------------------


async def test_alias_file_path_becomes_path() -> None:
    received: dict[str, object] = {}

    def handler(path: str, text: str) -> str:
        received["path"] = path
        received["text"] = text
        return "ok"

    spec = ToolSpec(
        name="write_text_file",
        description="test",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
            "required": ["path", "text"],
        },
        handler=handler,  # type: ignore[arg-type]
        side_effects=SideEffects.MUTATING,
    )
    await call_tool_handler(spec, {"file_path": "/tmp/x", "text": "hi"})
    assert received["path"] == "/tmp/x"


async def test_alias_content_becomes_text() -> None:
    received: dict[str, object] = {}

    def handler(path: str, text: str) -> str:
        received["text"] = text
        return "ok"

    spec = ToolSpec(
        name="write_text_file",
        description="test",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
            "required": ["path", "text"],
        },
        handler=handler,  # type: ignore[arg-type]
        side_effects=SideEffects.MUTATING,
    )
    await call_tool_handler(spec, {"path": "/tmp/x", "content": "world"})
    assert received["text"] == "world"


async def test_canonical_wins_when_alias_and_canonical_both_present() -> None:
    received: dict[str, object] = {}

    def handler(path: str, text: str) -> str:
        received["text"] = text
        return "ok"

    spec = ToolSpec(
        name="write_text_file",
        description="test",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
            "required": ["path", "text"],
        },
        handler=handler,  # type: ignore[arg-type]
        side_effects=SideEffects.MUTATING,
    )
    await call_tool_handler(
        spec, {"path": "/x", "text": "canonical", "content": "alias"}
    )
    assert received["text"] == "canonical"


# ---------------------------------------------------------------------------
# Step 6: graceful kwarg dropout
# ---------------------------------------------------------------------------


async def test_unexpected_kwarg_dropped_and_retried() -> None:
    def handler(x: str) -> str:
        return f"got:{x}"

    # handler doesn't accept 'extra' — bridge should drop it silently
    result = await call_tool_handler(
        _spec(
            "t",
            handler,
            params={"x": {"type": "string"}, "extra": {"type": "string"}},
            required=["x"],
        ),
        {"x": "hi", "extra": "unwanted"},
    )
    assert result == "got:hi"


# ---------------------------------------------------------------------------
# maybe_truncate_result
# ---------------------------------------------------------------------------


async def test_small_result_passes_through() -> None:
    short = "x" * 100
    result = await call_tool_handler(_spec("t", lambda: short), {})
    assert result == short


def test_maybe_truncate_result_large_input(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """maybe_truncate_result trims >200 KB output and writes full text to disk."""
    from obscura.core import tool_bridge
    from obscura.core.tool_bridge import maybe_truncate_result

    monkeypatch.setattr(tool_bridge, "TOOL_RESULT_CACHE_DIR", tmp_path)
    big = "a\n" * (200 * 1024)  # >> 200 KB

    result = maybe_truncate_result(big, tool_name="big_tool", tool_use_id="tu-test-123")
    assert "[Result truncated" in result
    assert len(result.encode()) < len(big.encode())
