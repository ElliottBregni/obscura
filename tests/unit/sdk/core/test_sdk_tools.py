"""Tests for sdk.internal.tools — @tool decorator, ToolRegistry, schema inference."""

from __future__ import annotations

from sdk.internal.tools import ToolRegistry, _infer_schema_from_hints, tool


# ---------------------------------------------------------------------------
# Schema inference
# ---------------------------------------------------------------------------

class TestSchemaInference:
    def test_simple_types(self) -> None:
        def fn(name: str, count: int, ratio: float, flag: bool) -> str:
            return ""

        schema = _infer_schema_from_hints(fn)
        assert schema["type"] == "object"
        assert schema["properties"]["name"] == {"type": "string"}
        assert schema["properties"]["count"] == {"type": "integer"}
        assert schema["properties"]["ratio"] == {"type": "number"}
        assert schema["properties"]["flag"] == {"type": "boolean"}
        assert set(schema["required"]) == {"name", "count", "ratio", "flag"}

    def test_default_params_not_required(self) -> None:
        def fn(required_arg: str, optional_arg: str = "default") -> str:
            return ""

        schema = _infer_schema_from_hints(fn)
        assert "required_arg" in schema["required"]
        assert "optional_arg" not in schema["required"]

    def test_no_params(self) -> None:
        def fn() -> None:
            pass

        schema = _infer_schema_from_hints(fn)
        assert schema["properties"] == {}
        assert schema["required"] == []

    def test_skips_self(self) -> None:
        def fn(self, x: str) -> str:  # noqa: N805
            return x

        schema = _infer_schema_from_hints(fn)
        assert "self" not in schema["properties"]
        assert "x" in schema["properties"]


# ---------------------------------------------------------------------------
# @tool decorator
# ---------------------------------------------------------------------------

class TestToolDecorator:
    def test_basic(self) -> None:
        @tool("greet", "Greet a user", {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        })
        def greet(name: str) -> str:
            return f"Hello, {name}!"

        assert hasattr(greet, "spec")
        assert greet.spec.name == "greet"
        assert greet.spec.description == "Greet a user"
        assert greet.spec.parameters["required"] == ["name"]

    def test_function_still_callable(self) -> None:
        @tool("add", "Add numbers")
        def add(a: int, b: int) -> int:
            return a + b

        assert add(2, 3) == 5

    def test_auto_infer_schema(self) -> None:
        @tool("echo", "Echo back input")
        def echo(text: str) -> str:
            return text

        assert echo.spec.parameters["properties"]["text"] == {"type": "string"}
        assert "text" in echo.spec.parameters["required"]

    def test_explicit_schema_overrides_inference(self) -> None:
        @tool("custom", "Custom tool", {"type": "object", "properties": {"x": {"type": "number"}}})
        def custom(x: str) -> str:  # type hint says str, but schema says number
            return x

        assert custom.spec.parameters["properties"]["x"]["type"] == "number"

    def test_handler_reference(self) -> None:
        def my_fn(x: str) -> str:
            return x

        decorated = tool("t", "d")(my_fn)
        # Handler points to the original function
        assert decorated.spec.handler is my_fn


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class TestToolRegistry:
    def test_register_and_get(self) -> None:
        reg = ToolRegistry()

        @tool("test_tool", "A tool")
        def fn(x: str) -> str:
            return x

        reg.register(fn.spec)
        assert reg.get("test_tool") is fn.spec
        assert reg.get("nonexistent") is None

    def test_all(self) -> None:
        reg = ToolRegistry()

        @tool("a", "A")
        def a() -> None:
            pass

        @tool("b", "B")
        def b() -> None:
            pass

        reg.register(a.spec)
        reg.register(b.spec)
        assert len(reg.all()) == 2

    def test_names(self) -> None:
        reg = ToolRegistry()

        @tool("alpha", "Alpha")
        def alpha() -> None:
            pass

        reg.register(alpha.spec)
        assert reg.names() == ["alpha"]

    def test_contains(self) -> None:
        reg = ToolRegistry()

        @tool("t", "T")
        def t() -> None:
            pass

        reg.register(t.spec)
        assert "t" in reg
        assert "missing" not in reg

    def test_len(self) -> None:
        reg = ToolRegistry()
        assert len(reg) == 0

        @tool("t", "T")
        def t() -> None:
            pass

        reg.register(t.spec)
        assert len(reg) == 1
