"""Tests for sdk._tools — ToolRegistry, @tool decorator, schema inference."""
import pytest
from unittest.mock import patch, MagicMock

from sdk._tools import ToolRegistry, tool, _infer_schema_from_hints
from sdk._types import ToolSpec


class TestToolRegistry:
    def test_register_and_get(self):
        reg = ToolRegistry()
        spec = ToolSpec(name="t1", description="desc", parameters={}, handler=lambda: None)
        reg.register(spec)
        assert reg.get("t1") is spec

    def test_get_missing(self):
        reg = ToolRegistry()
        assert reg.get("missing") is None

    def test_all(self):
        reg = ToolRegistry()
        reg.register(ToolSpec(name="t1", description="d1", parameters={}, handler=lambda: None))
        reg.register(ToolSpec(name="t2", description="d2", parameters={}, handler=lambda: None))
        assert len(reg.all()) == 2

    def test_names(self):
        reg = ToolRegistry()
        reg.register(ToolSpec(name="t1", description="d1", parameters={}, handler=lambda: None))
        assert "t1" in reg.names()

    def test_len(self):
        reg = ToolRegistry()
        assert len(reg) == 0
        reg.register(ToolSpec(name="t1", description="d1", parameters={}, handler=lambda: None))
        assert len(reg) == 1

    def test_contains(self):
        reg = ToolRegistry()
        reg.register(ToolSpec(name="t1", description="d1", parameters={}, handler=lambda: None))
        assert "t1" in reg
        assert "t2" not in reg


class TestInferSchema:
    def test_simple_types(self):
        def fn(name: str, count: int, ratio: float, flag: bool):
            pass
        schema = _infer_schema_from_hints(fn)
        assert schema["properties"]["name"]["type"] == "string"
        assert schema["properties"]["count"]["type"] == "integer"
        assert schema["properties"]["ratio"]["type"] == "number"
        assert schema["properties"]["flag"]["type"] == "boolean"
        assert set(schema["required"]) == {"name", "count", "ratio", "flag"}

    def test_optional_params(self):
        def fn(name: str, count: int = 5):
            pass
        schema = _infer_schema_from_hints(fn)
        assert "name" in schema["required"]
        assert "count" not in schema["required"]

    def test_no_hints(self):
        def fn(x, y):
            pass
        schema = _infer_schema_from_hints(fn)
        assert schema["properties"]["x"]["type"] == "string"
        assert schema["properties"]["y"]["type"] == "string"


class TestToolDecorator:
    def test_explicit_schema(self):
        @tool("read_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}})
        def read_file(path: str) -> str:
            return f"content of {path}"

        assert hasattr(read_file, "spec")
        assert read_file.spec.name == "read_file"
        assert read_file.spec.description == "Read a file"

    def test_inferred_schema(self):
        @tool("add", "Add two numbers")
        def add(a: int, b: int) -> int:
            return a + b

        assert add.spec.parameters["properties"]["a"]["type"] == "integer"
        assert add.spec.parameters["properties"]["b"]["type"] == "integer"

    def test_sync_wrapper_calls_fn(self):
        @tool("greet", "Greet someone")
        def greet(name: str) -> str:
            return f"Hello {name}"

        # OTel won't be available, should fall back to direct call
        result = greet(name="World")
        assert result == "Hello World"

    @pytest.mark.asyncio
    async def test_async_wrapper_calls_fn(self):
        @tool("async_greet", "Greet async")
        async def async_greet(name: str) -> str:
            return f"Hello {name}"

        result = await async_greet(name="World")
        assert result == "Hello World"

    def test_pydantic_model_schema(self):
        from pydantic import BaseModel

        class ReadParams(BaseModel):
            path: str
            encoding: str = "utf-8"

        @tool("read", "Read file", pydantic_model=ReadParams)
        def read(path: str, encoding: str = "utf-8") -> str:
            return ""

        assert "path" in read.spec.parameters["properties"]
