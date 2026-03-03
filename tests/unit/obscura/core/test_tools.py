"""Tests for sdk.internal.tools — ToolRegistry, @tool decorator, schema inference."""

from typing import Any

import pytest

from obscura.core.tools import ToolRegistry, tool, infer_schema_from_hints
from obscura.core.types import ToolSpec


class TestToolRegistry:
    def test_register_and_get(self) -> None:
        reg = ToolRegistry()
        spec = ToolSpec(
            name="t1", description="desc", parameters={}, handler=lambda: None
        )
        reg.register(spec)
        assert reg.get("t1") is spec

    def test_get_missing(self) -> None:
        reg = ToolRegistry()
        assert reg.get("missing") is None

    def test_all(self) -> None:
        reg = ToolRegistry()
        reg.register(
            ToolSpec(name="t1", description="d1", parameters={}, handler=lambda: None)
        )
        reg.register(
            ToolSpec(name="t2", description="d2", parameters={}, handler=lambda: None)
        )
        assert len(reg.all()) == 2

    def test_names(self) -> None:
        reg = ToolRegistry()
        reg.register(
            ToolSpec(name="t1", description="d1", parameters={}, handler=lambda: None)
        )
        assert "t1" in reg.names()

    def test_len(self) -> None:
        reg = ToolRegistry()
        assert len(reg) == 0
        reg.register(
            ToolSpec(name="t1", description="d1", parameters={}, handler=lambda: None)
        )
        assert len(reg) == 1

    def test_contains(self) -> None:
        reg = ToolRegistry()
        reg.register(
            ToolSpec(name="t1", description="d1", parameters={}, handler=lambda: None)
        )
        assert "t1" in reg
        assert "t2" not in reg

    def test_get_functions_prefixed_tool_name(self) -> None:
        reg = ToolRegistry()
        spec = ToolSpec(
            name="web_search",
            description="desc",
            parameters={},
            handler=lambda: None,
        )
        reg.register(spec)
        assert reg.get("functions.web_search") is spec
        assert reg.get("multi_tool_use.web_search") is spec


class TestInferSchema:
    def test_simple_types(self) -> None:
        def fn(name: str, count: int, ratio: float, flag: bool) -> None:
            pass

        schema: dict[str, Any] = infer_schema_from_hints(fn)
        assert schema["properties"]["name"]["type"] == "string"
        assert schema["properties"]["count"]["type"] == "integer"
        assert schema["properties"]["ratio"]["type"] == "number"
        assert schema["properties"]["flag"]["type"] == "boolean"
        assert set(schema["required"]) == {"name", "count", "ratio", "flag"}

    def test_optional_params(self) -> None:
        def fn(name: str, count: int = 5) -> None:
            pass

        schema: dict[str, Any] = infer_schema_from_hints(fn)
        assert "name" in schema["required"]
        assert "count" not in schema["required"]

    def test_no_hints(self) -> None:
        def fn(x: Any, y: Any) -> None:
            pass

        schema: dict[str, Any] = infer_schema_from_hints(fn)
        assert schema["properties"]["x"]["type"] == "string"
        assert schema["properties"]["y"]["type"] == "string"


class TestToolDecorator:
    def test_explicit_schema(self) -> None:
        @tool(
            "read_file",
            "Read a file",
            {"type": "object", "properties": {"path": {"type": "string"}}},
        )
        def read_file(path: str) -> str:
            return f"content of {path}"

        assert hasattr(read_file, "spec")
        spec: Any = getattr(read_file, "spec")
        assert spec.name == "read_file"
        assert spec.description == "Read a file"

    def test_inferred_schema(self) -> None:
        @tool("add", "Add two numbers")
        def add(a: int, b: int) -> int:
            return a + b

        spec: Any = getattr(add, "spec")
        assert spec.parameters["properties"]["a"]["type"] == "integer"
        assert spec.parameters["properties"]["b"]["type"] == "integer"

    def test_sync_wrapper_calls_fn(self) -> None:
        @tool("greet", "Greet someone")
        def greet(name: str) -> str:
            return f"Hello {name}"

        # OTel won't be available, should fall back to direct call
        result = greet(name="World")
        assert result == "Hello World"

    @pytest.mark.asyncio
    async def test_async_wrapper_calls_fn(self) -> None:
        @tool("async_greet", "Greet async")
        async def async_greet(name: str) -> str:
            return f"Hello {name}"

        result = await async_greet(name="World")
        assert result == "Hello World"

    def test_pydantic_model_schema(self) -> None:
        from pydantic import BaseModel

        class ReadParams(BaseModel):
            path: str
            encoding: str = "utf-8"

        @tool("read", "Read file", pydantic_model=ReadParams)
        def read(path: str, encoding: str = "utf-8") -> str:
            return ""

        spec: Any = getattr(read, "spec")
        assert "path" in spec.parameters["properties"]
