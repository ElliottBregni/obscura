"""Tests for ``obscura.providers._mcp_execution_bridge``.

The bridge replaces error-returning shadow handlers with real handlers that
dispatch to a persistent ``MCPClient`` session. Tests cover:

* Name parsing (``mcp__server__tool`` → ``(server, tool)``).
* Lifecycle: ``start`` opens sessions, ``stop`` closes them, partial failures
  don't poison the rest.
* Handler installation: only swaps handlers for shadows whose server connected
  successfully; leaves the rest alone.
* Handler dispatch: routes to the right session, converts MCPToolResult, and
  surfaces errors as structured payloads instead of raising.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from obscura.core.tools import ToolRegistry
from obscura.core.types import ToolSpec
from obscura.integrations.mcp.types import MCPToolResult
from obscura.providers._mcp_execution_bridge import (
    MCPExecutionBridge,
    _parse_qualified_name,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _shadow_spec(name: str) -> ToolSpec:
    """Build a stand-in shadow spec (handler is a noop async lambda)."""

    async def _noop(**_kwargs: Any) -> str:
        return "shadow"

    return ToolSpec(
        name=name,
        description=f"shadow for {name}",
        parameters={"type": "object", "properties": {}},
        handler=_noop,
    )


class _FakeBackend:
    """Minimal stand-in for a backend with the bridge's required attrs."""

    def __init__(self, specs: list[ToolSpec]) -> None:
        self._tools: list[ToolSpec] = list(specs)
        self._tool_registry = ToolRegistry()
        for s in specs:
            self._tool_registry.register(s)
        report = MagicMock()
        report.specs = list(specs)
        self.last_mcp_discovery_report = report


def _stdio_server(name: str) -> dict[str, Any]:
    return {"name": name, "transport": "stdio", "command": "true"}


# ---------------------------------------------------------------------------
# 1. _parse_qualified_name
# ---------------------------------------------------------------------------


class TestParseQualifiedName:
    def test_simple(self) -> None:
        assert _parse_qualified_name("mcp__forge__generate_image") == (
            "forge",
            "generate_image",
        )

    def test_tool_name_with_double_underscores(self) -> None:
        # Tool name "list__models" is split correctly: server is the FIRST
        # double-underscore segment, tool is the rest.
        assert _parse_qualified_name("mcp__forge__list__models") == (
            "forge",
            "list__models",
        )

    def test_missing_prefix(self) -> None:
        assert _parse_qualified_name("forge__generate_image") is None

    def test_missing_tool(self) -> None:
        assert _parse_qualified_name("mcp__forge") is None

    def test_empty_segments(self) -> None:
        assert _parse_qualified_name("mcp____tool") is None
        assert _parse_qualified_name("mcp__server__") is None


# ---------------------------------------------------------------------------
# 2. Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_with_no_servers_is_noop(self) -> None:
        b = MCPExecutionBridge([])
        await b.start()
        assert b.started is True
        assert b.connected_servers == frozenset()

    @pytest.mark.asyncio
    async def test_start_idempotent(self) -> None:
        b = MCPExecutionBridge([])
        await b.start()
        await b.start()  # Second call must not blow up.
        assert b.started is True

    @pytest.mark.asyncio
    async def test_start_skips_unnamed_server(self, monkeypatch: Any) -> None:
        # Server with no "name" key gets skipped, others succeed.
        b = MCPExecutionBridge(
            [{"transport": "stdio", "command": "true"}, _stdio_server("ok")]
        )
        monkeypatch.setattr(
            b._manager, "add_session", AsyncMock(return_value=MagicMock())
        )
        await b.start()
        assert b.connected_servers == {"ok"}

    @pytest.mark.asyncio
    async def test_start_skips_malformed_server(self, monkeypatch: Any) -> None:
        # stdio server with no command → _build_config returns None → skip.
        b = MCPExecutionBridge(
            [
                {"name": "broken", "transport": "stdio"},
                _stdio_server("ok"),
            ]
        )
        monkeypatch.setattr(
            b._manager, "add_session", AsyncMock(return_value=MagicMock())
        )
        await b.start()
        assert b.connected_servers == {"ok"}

    @pytest.mark.asyncio
    async def test_start_continues_on_connect_failure(self, monkeypatch: Any) -> None:
        b = MCPExecutionBridge([_stdio_server("alpha"), _stdio_server("beta")])

        async def fake_add(name: str, _config: Any) -> Any:
            if name == "alpha":
                raise RuntimeError("connect refused")
            return MagicMock()

        monkeypatch.setattr(b._manager, "add_session", AsyncMock(side_effect=fake_add))
        await b.start()
        assert b.connected_servers == {"beta"}

    @pytest.mark.asyncio
    async def test_stop_closes_all(self, monkeypatch: Any) -> None:
        b = MCPExecutionBridge([_stdio_server("ok")])
        monkeypatch.setattr(
            b._manager, "add_session", AsyncMock(return_value=MagicMock())
        )
        close_all = AsyncMock()
        monkeypatch.setattr(b._manager, "close_all", close_all)
        await b.start()
        await b.stop()
        close_all.assert_awaited_once()
        assert b.started is False
        assert b.connected_servers == frozenset()

    @pytest.mark.asyncio
    async def test_stop_without_start_is_safe(self) -> None:
        b = MCPExecutionBridge([_stdio_server("ok")])
        await b.stop()  # Must not raise.

    @pytest.mark.asyncio
    async def test_stop_swallows_close_errors(self, monkeypatch: Any) -> None:
        b = MCPExecutionBridge([_stdio_server("ok")])
        monkeypatch.setattr(
            b._manager, "add_session", AsyncMock(return_value=MagicMock())
        )
        monkeypatch.setattr(
            b._manager, "close_all", AsyncMock(side_effect=RuntimeError("kaboom"))
        )
        await b.start()
        await b.stop()  # Errors during teardown must not propagate.
        assert b.started is False


# ---------------------------------------------------------------------------
# 3. install_handlers
# ---------------------------------------------------------------------------


class TestInstallHandlers:
    @pytest.mark.asyncio
    async def test_swaps_handler_in_registry_and_list(self, monkeypatch: Any) -> None:
        spec = _shadow_spec("mcp__forge__generate_image")
        backend = _FakeBackend([spec])
        bridge = MCPExecutionBridge([_stdio_server("forge")])
        monkeypatch.setattr(
            bridge._manager, "add_session", AsyncMock(return_value=MagicMock())
        )
        await bridge.start()

        installed = bridge.install_handlers(backend)
        assert installed == 1

        # The list entry was replaced with a NEW spec (different handler).
        new_spec = backend._tools[0]
        assert new_spec.name == spec.name
        assert new_spec.handler is not spec.handler

        # The registry returns the replaced spec for the same name.
        from_registry = backend._tool_registry.get(spec.name)
        assert from_registry is new_spec

    def test_no_report_returns_zero(self) -> None:
        bridge = MCPExecutionBridge([])
        backend = MagicMock()
        backend.last_mcp_discovery_report = None
        assert bridge.install_handlers(backend) == 0

    def test_no_connected_servers_returns_zero(self) -> None:
        backend = _FakeBackend([_shadow_spec("mcp__forge__x")])
        bridge = MCPExecutionBridge([])  # never started, no connections
        assert bridge.install_handlers(backend) == 0

    @pytest.mark.asyncio
    async def test_skips_specs_for_unconnected_servers(self, monkeypatch: Any) -> None:
        connected_spec = _shadow_spec("mcp__connected__x")
        unconnected_spec = _shadow_spec("mcp__missing__y")
        backend = _FakeBackend([connected_spec, unconnected_spec])

        bridge = MCPExecutionBridge([_stdio_server("connected")])
        monkeypatch.setattr(
            bridge._manager, "add_session", AsyncMock(return_value=MagicMock())
        )
        await bridge.start()

        assert bridge.install_handlers(backend) == 1
        # The unconnected spec is left untouched.
        assert backend._tools[1] is unconnected_spec

    @pytest.mark.asyncio
    async def test_skips_unparseable_names(self, monkeypatch: Any) -> None:
        ok_spec = _shadow_spec("mcp__forge__x")
        garbage_spec = _shadow_spec("not_an_mcp_tool")
        backend = _FakeBackend([ok_spec, garbage_spec])

        bridge = MCPExecutionBridge([_stdio_server("forge")])
        monkeypatch.setattr(
            bridge._manager, "add_session", AsyncMock(return_value=MagicMock())
        )
        await bridge.start()

        assert bridge.install_handlers(backend) == 1
        assert backend._tools[1] is garbage_spec


# ---------------------------------------------------------------------------
# 4. Handler dispatch
# ---------------------------------------------------------------------------


class TestHandlerDispatch:
    @pytest.mark.asyncio
    async def test_handler_routes_to_session(self, monkeypatch: Any) -> None:
        spec = _shadow_spec("mcp__forge__generate_image")
        backend = _FakeBackend([spec])

        fake_client = MagicMock()
        fake_client.call_tool = AsyncMock(
            return_value=MCPToolResult(
                content=[{"type": "text", "text": "ok"}],
                isError=False,
            )
        )
        bridge = MCPExecutionBridge([_stdio_server("forge")])
        monkeypatch.setattr(
            bridge._manager, "add_session", AsyncMock(return_value=fake_client)
        )

        # add_session is mocked, so we have to put the client into the
        # manager's internal map manually for get_session() to find it.
        async def _populate(name: str, _config: Any) -> Any:
            bridge._manager._sessions[name] = fake_client
            return fake_client

        monkeypatch.setattr(
            bridge._manager, "add_session", AsyncMock(side_effect=_populate)
        )

        await bridge.start()
        bridge.install_handlers(backend)

        new_spec = backend._tool_registry.get(spec.name)
        assert new_spec is not None
        result = await new_spec.handler(prompt="a red apple")
        assert result == "ok"
        fake_client.call_tool.assert_awaited_once_with(
            "generate_image", {"prompt": "a red apple"}
        )

    @pytest.mark.asyncio
    async def test_handler_returns_error_when_session_dropped(
        self, monkeypatch: Any
    ) -> None:
        spec = _shadow_spec("mcp__forge__x")
        backend = _FakeBackend([spec])

        # Connect successfully, then yank the session out from under the
        # bridge before the handler fires.
        fake_client = MagicMock()
        bridge = MCPExecutionBridge([_stdio_server("forge")])

        async def _populate(name: str, _config: Any) -> Any:
            bridge._manager._sessions[name] = fake_client
            return fake_client

        monkeypatch.setattr(
            bridge._manager, "add_session", AsyncMock(side_effect=_populate)
        )
        await bridge.start()
        bridge.install_handlers(backend)

        # Drop the session.
        bridge._manager._sessions.pop("forge")

        new_spec = backend._tool_registry.get(spec.name)
        assert new_spec is not None
        result = await new_spec.handler()
        assert isinstance(result, dict)
        assert result["error"] == "mcp_session_unavailable"

    @pytest.mark.asyncio
    async def test_handler_returns_error_on_call_failure(
        self, monkeypatch: Any
    ) -> None:
        spec = _shadow_spec("mcp__forge__x")
        backend = _FakeBackend([spec])

        fake_client = MagicMock()
        fake_client.call_tool = AsyncMock(side_effect=RuntimeError("upstream 503"))
        bridge = MCPExecutionBridge([_stdio_server("forge")])

        async def _populate(name: str, _config: Any) -> Any:
            bridge._manager._sessions[name] = fake_client
            return fake_client

        monkeypatch.setattr(
            bridge._manager, "add_session", AsyncMock(side_effect=_populate)
        )
        await bridge.start()
        bridge.install_handlers(backend)

        new_spec = backend._tool_registry.get(spec.name)
        assert new_spec is not None
        result = await new_spec.handler()
        assert isinstance(result, dict)
        assert result["error"] == "mcp_call_failed"
        assert result["server"] == "forge"
        assert result["tool"] == "x"
        assert "upstream 503" in result["detail"]
