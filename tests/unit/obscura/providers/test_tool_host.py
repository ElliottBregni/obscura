"""Tests for BackendToolHostMixin — the dedup'd register_tool used by every backend."""

from __future__ import annotations

import pytest

from obscura.core.tools import ToolRegistry, tool
from obscura.providers._tool_host import BackendToolHostMixin


class _FakeBackend(BackendToolHostMixin):
    """Minimal concrete subclass for unit testing the mixin."""

    def __init__(self) -> None:
        self._init_tool_host()


@tool("alpha", "alpha tool")
def _alpha() -> str:
    return ""


@tool("beta", "beta tool")
def _beta() -> str:
    return ""


class TestBackendToolHostMixin:
    def test_init_creates_empty_state(self) -> None:
        b = _FakeBackend()
        assert b._tools == []
        assert isinstance(b._tool_registry, ToolRegistry)
        assert b.tool_specs == ()

    def test_register_adds_to_both_stores(self) -> None:
        b = _FakeBackend()
        b.register_tool(_alpha.spec)
        assert b._tools == [_alpha.spec]
        assert b._tool_registry.get("alpha") is _alpha.spec

    def test_register_skips_duplicates(self) -> None:
        b = _FakeBackend()
        b.register_tool(_alpha.spec)
        b.register_tool(_alpha.spec)
        assert len(b._tools) == 1

    def test_register_preserves_insertion_order(self) -> None:
        b = _FakeBackend()
        b.register_tool(_alpha.spec)
        b.register_tool(_beta.spec)
        assert [s.name for s in b.tool_specs] == ["alpha", "beta"]

    def test_get_tool_registry_returns_registry(self) -> None:
        b = _FakeBackend()
        assert b.get_tool_registry() is b._tool_registry

    def test_init_tool_host_idempotent(self) -> None:
        """Calling _init_tool_host twice doesn't reset existing state."""
        b = _FakeBackend()
        b.register_tool(_alpha.spec)
        b._init_tool_host()
        assert _alpha.spec in b._tools


class TestEveryBackendUsesMixin:
    """Smoke test: each concrete backend exposes the mixin's API."""

    @pytest.mark.parametrize(
        "module_path,class_name",
        [
            ("obscura.providers.claude", "ClaudeBackend"),
            ("obscura.providers.copilot", "CopilotBackend"),
            ("obscura.providers.codex", "CodexBackend"),
            ("obscura.providers.openai", "OpenAIBackend"),
            ("obscura.providers.localllm", "LocalLLMBackend"),
        ],
    )
    def test_inherits_mixin(self, module_path: str, class_name: str) -> None:
        import importlib

        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        assert issubclass(cls, BackendToolHostMixin), (
            f"{class_name} should inherit BackendToolHostMixin"
        )
