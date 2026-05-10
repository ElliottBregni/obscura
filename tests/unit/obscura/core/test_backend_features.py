from __future__ import annotations

from obscura.core.backend_features import backend_routes_mcp_natively
from obscura.core.enums.agent import Backend


def test_codex_routes_mcp_natively() -> None:
    assert backend_routes_mcp_natively(Backend.CODEX)
    assert backend_routes_mcp_natively("codex")


def test_non_codex_backends_use_obscura_mcp_executor() -> None:
    assert not backend_routes_mcp_natively(Backend.CLAUDE)
    assert not backend_routes_mcp_natively("openai")
    assert not backend_routes_mcp_natively(None)
