"""Verify ObscuraClient routes MCP server configs correctly per backend.

Codex runs its own closed tool loop and can't reach Obscura's executor,
so its backend must receive ``mcp_servers`` directly. Every other
backend lets Obscura's MCPBackend re-expose the tools via the registry
and therefore receives ``mcp_servers=None``.
"""
# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownMemberType=false

from __future__ import annotations

from typing import Any

import pytest

from obscura.core.auth import AuthConfig
from obscura.core.client import ObscuraClient


@pytest.fixture
def mcp_configs() -> list[dict[str, Any]]:
    return [{"name": "browser", "url": "http://127.0.0.1:12345/mcp"}]


def _auth_for(backend: str) -> AuthConfig:
    """Produce an AuthConfig that satisfies each backend's boot checks."""
    if backend == "claude":
        return AuthConfig(anthropic_api_key="test-key")
    if backend == "copilot":
        return AuthConfig(github_token="test-token")
    return AuthConfig()


class TestMcpForwarding:
    def test_codex_receives_mcp_servers_directly(
        self,
        mcp_configs: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Codex backend should be constructed with our mcp_servers list."""
        captured: dict[str, Any] = {}

        original = ObscuraClient._create_backend

        def capture(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return original(**kwargs)

        monkeypatch.setattr(ObscuraClient, "_create_backend", staticmethod(capture))

        client = ObscuraClient(
            "codex",
            auth=_auth_for("codex"),
            mcp_servers=mcp_configs,
        )
        assert captured["mcp_servers"] == mcp_configs
        # And: MCPBackend should NOT spin up for Codex (empty config list).
        assert client._mcp_server_configs == []

    @pytest.mark.parametrize("backend", ["copilot", "claude"])
    def test_non_codex_backends_do_not_receive_mcp_servers(
        self,
        backend: str,
        mcp_configs: list[dict[str, Any]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Claude/Copilot keep the existing path: backend gets None, MCPBackend
        inherits the configs instead."""
        captured: dict[str, Any] = {}

        original = ObscuraClient._create_backend

        def capture(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return original(**kwargs)

        monkeypatch.setattr(ObscuraClient, "_create_backend", staticmethod(capture))

        client = ObscuraClient(
            backend,
            auth=_auth_for(backend),
            mcp_servers=mcp_configs,
        )
        assert captured["mcp_servers"] is None
        assert client._mcp_server_configs == mcp_configs
