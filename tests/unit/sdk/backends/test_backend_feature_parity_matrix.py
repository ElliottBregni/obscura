"""Parity-oriented backend contract tests.

These tests verify each backend exposes the same core runtime surface while
still advertising provider-native feature flags.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from sdk.backends.claude import ClaudeBackend
from sdk.backends.copilot import CopilotBackend
from sdk.backends.localllm import LocalLLMBackend
from sdk.backends.openai_compat import OpenAIBackend
from sdk.internal.auth import AuthConfig
from sdk.internal.types import HookPoint, NativeHandle, ToolSpec


def _auth_for(name: str) -> AuthConfig:
    if name == "openai":
        return AuthConfig(openai_api_key="sk-test")
    if name == "localllm":
        return AuthConfig(localllm_base_url="http://localhost:1234/v1")
    if name == "claude":
        return AuthConfig(anthropic_api_key="sk-ant-test")
    return AuthConfig(github_token="gh-test")


@pytest.mark.parametrize(
    ("name", "backend_cls"),
    [
        ("openai", OpenAIBackend),
        ("localllm", LocalLLMBackend),
        ("claude", ClaudeBackend),
        ("copilot", CopilotBackend),
    ],
)
def test_backend_core_surface(name: str, backend_cls: Any) -> None:
    backend = backend_cls(_auth_for(name))

    # Unified backend protocol surface
    for method in (
        "start",
        "stop",
        "send",
        "stream",
        "create_session",
        "resume_session",
        "list_sessions",
        "delete_session",
        "register_tool",
        "register_hook",
        "get_tool_registry",
        "capabilities",
    ):
        assert hasattr(backend, method), f"{name} missing {method}"

    caps = backend.capabilities()
    assert caps.supports_native_mode is True
    assert len(caps.native_features) > 0

    native = backend.native
    assert isinstance(native, NativeHandle)


@pytest.mark.parametrize(
    ("name", "backend_cls"),
    [
        ("openai", OpenAIBackend),
        ("localllm", LocalLLMBackend),
        ("claude", ClaudeBackend),
        ("copilot", CopilotBackend),
    ],
)
def test_backend_registers_tools_and_hooks(name: str, backend_cls: Any) -> None:
    backend = backend_cls(_auth_for(name))

    def _echo(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    spec = ToolSpec(
        name="echo",
        description="Echo input",
        parameters={"type": "object"},
        handler=_echo,
    )
    backend.register_tool(spec)
    backend.register_hook(HookPoint.STOP, lambda _ctx: None)

    if hasattr(backend, "tools"):
        tools = getattr(backend, "tools")
        assert isinstance(tools, list)
        tools_any = cast(list[Any], tools)
        assert any(getattr(t, "name", "") == "echo" for t in tools_any)

    if hasattr(backend, "hooks"):
        hooks = getattr(backend, "hooks")
        assert HookPoint.STOP in hooks
