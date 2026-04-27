"""End-to-end socket bridge tests.

Spins up a ``SocketBridge`` against a stub call-dispatcher, connects a real
``BrowserBridgeClient`` over a Unix socket, and exercises the protocol.
No Chrome involved.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from obscura.integrations.browser import active_hosts
from obscura.integrations.browser.client import (
    BrowserBridgeClient,
    BrowserBridgeError,
)
from obscura.integrations.browser.server import SocketBridge


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """A short tmpdir under /tmp — the default tmp_path exceeds AF_UNIX's
    104-byte path limit on macOS."""
    d = Path("/tmp") / f"obs-br-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=False)
    try:
        yield d
    finally:
        for p in d.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            d.rmdir()
        except OSError:
            pass


def _stub_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": "browser_read_page",
            "description": "stub",
            "parameters": {"type": "object", "properties": {}},
            "side_effects": "none",
        },
        {
            "name": "browser_navigate",
            "description": "stub",
            "parameters": {"type": "object", "properties": {}},
            "side_effects": "mutating",
        },
    ]


@pytest.mark.asyncio
async def test_handshake_and_list_tools(short_tmp: Path) -> None:
    sock = short_tmp / "bridge.sock"

    async def call(_name: str, _args: dict[str, Any]) -> Any:
        return {"unused": True}

    bridge = SocketBridge(
        path=sock,
        tools_provider=_stub_specs,
        call=call,
        profile_id=lambda: "test-profile",
    )
    await bridge.start()
    try:
        client = await BrowserBridgeClient.connect(socket_path=sock)
        try:
            tools = await client.list_tools()
            names = [t["name"] for t in tools]
            assert names == ["browser_read_page", "browser_navigate"]
        finally:
            await client.close()
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_call_dispatches_to_handler(short_tmp: Path) -> None:
    sock = short_tmp / "bridge.sock"
    seen: list[tuple[str, dict[str, Any]]] = []

    async def call(name: str, args: dict[str, Any]) -> Any:
        seen.append((name, args))
        return {"echo": args}

    bridge = SocketBridge(path=sock, tools_provider=_stub_specs, call=call)
    await bridge.start()
    try:
        async with await BrowserBridgeClient.connect(socket_path=sock) as client:
            value = await client.call(
                "browser_read_page", {"max_chars": 1234}, timeout=2.0
            )
        assert seen == [("browser_read_page", {"max_chars": 1234})]
        assert value == {"echo": {"max_chars": 1234}}
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_call_propagates_handler_exception(short_tmp: Path) -> None:
    sock = short_tmp / "bridge.sock"

    async def call(_name: str, _args: dict[str, Any]) -> Any:
        msg = "boom"
        raise RuntimeError(msg)

    bridge = SocketBridge(path=sock, tools_provider=_stub_specs, call=call)
    await bridge.start()
    try:
        async with await BrowserBridgeClient.connect(socket_path=sock) as client:
            with pytest.raises(BrowserBridgeError, match="boom"):
                await client.call("browser_read_page", {}, timeout=2.0)
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_concurrent_calls_multiplex(short_tmp: Path) -> None:
    """Multiple in-flight calls on one connection must each get the right reply."""
    sock = short_tmp / "bridge.sock"

    async def call(name: str, args: dict[str, Any]) -> Any:
        # Sleep proportional to arg so the responses arrive out of order
        # relative to the requests.
        await asyncio.sleep(args.get("ms", 0) / 1000.0)
        return {"name": name, "ms": args.get("ms")}

    bridge = SocketBridge(path=sock, tools_provider=_stub_specs, call=call)
    await bridge.start()
    try:
        async with await BrowserBridgeClient.connect(socket_path=sock) as client:
            results = await asyncio.gather(
                client.call("browser_read_page", {"ms": 60}, timeout=5.0),
                client.call("browser_read_page", {"ms": 20}, timeout=5.0),
                client.call("browser_navigate", {"ms": 40}, timeout=5.0),
            )
        assert results[0]["ms"] == 60
        assert results[1]["ms"] == 20
        assert results[2]["ms"] == 40
        assert results[2]["name"] == "browser_navigate"
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_unknown_tool_returns_error(short_tmp: Path) -> None:
    sock = short_tmp / "bridge.sock"

    async def call(name: str, _args: dict[str, Any]) -> Any:
        msg = f"unknown tool: {name}"
        raise ValueError(msg)

    bridge = SocketBridge(path=sock, tools_provider=_stub_specs, call=call)
    await bridge.start()
    try:
        async with await BrowserBridgeClient.connect(socket_path=sock) as client:
            with pytest.raises(BrowserBridgeError, match="unknown tool"):
                await client.call("does_not_exist", {})
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_connect_fails_when_no_active_host(short_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        active_hosts, "default_registry_path", lambda: short_tmp / "active.json"
    )
    with pytest.raises(BrowserBridgeError, match="no active obscura browser host"):
        await BrowserBridgeClient.connect()


@pytest.mark.asyncio
async def test_discovery_via_registry(short_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Client discovers an active host via the registry file (no socket_path arg)."""
    sock = short_tmp / "bridge.sock"
    reg = short_tmp / "active.json"
    monkeypatch.setattr(
        active_hosts, "default_registry_path", lambda: reg
    )

    async def call(_name: str, _args: dict[str, Any]) -> Any:
        return {"ok": True}

    bridge = SocketBridge(path=sock, tools_provider=_stub_specs, call=call)
    await bridge.start()
    try:
        active_hosts.register(
            pid=os.getpid(),
            socket=sock,
            profile_id="from-registry",
            browser="chrome",
            version="0.1.0",
            path=reg,
        )
        async with await BrowserBridgeClient.connect() as client:
            assert client.host_entry.get("profile_id") == "from-registry"
            value = await client.call("browser_read_page", {}, timeout=2.0)
        assert value == {"ok": True}
    finally:
        active_hosts.unregister(pid=os.getpid(), path=reg)
        await bridge.stop()


@pytest.mark.asyncio
async def test_attach_if_running_returns_status(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """attach_if_running should auto-discover, attach, and report status."""
    from obscura.integrations.browser.client import attach_if_running

    sock = short_tmp / "bridge.sock"
    reg = short_tmp / "active.json"
    monkeypatch.setattr(active_hosts, "default_registry_path", lambda: reg)

    async def call(name: str, args: dict[str, Any]) -> Any:
        return {"name": name, "args": args}

    bridge = SocketBridge(path=sock, tools_provider=_stub_specs, call=call)
    await bridge.start()
    try:
        active_hosts.register(
            pid=os.getpid(),
            socket=sock,
            profile_id="abc-1234",
            browser="chrome",
            version="0.1.0",
            path=reg,
        )
        registered: list[Any] = []
        client, status = await attach_if_running(registered.append)
        try:
            assert client is not None
            assert status is not None
            assert status["browser"] == "chrome"
            assert status["profile_id"] == "abc-1234"
            assert status["tool_count"] == 2
            assert len(registered) == 2
            assert {s.name for s in registered} == {
                "browser_read_page",
                "browser_navigate",
            }
        finally:
            if client is not None:
                await client.close()
    finally:
        active_hosts.unregister(pid=os.getpid(), path=reg)
        await bridge.stop()


@pytest.mark.asyncio
async def test_attach_if_running_no_host_is_noop(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no host is registered, attach should return (None, None) silently."""
    from obscura.integrations.browser.client import attach_if_running

    monkeypatch.setattr(
        active_hosts, "default_registry_path", lambda: short_tmp / "missing.json"
    )
    registered: list[Any] = []
    client, status = await attach_if_running(registered.append)
    assert client is None
    assert status is None
    assert registered == []


@pytest.mark.asyncio
async def test_register_browser_tools_helper(short_tmp: Path) -> None:
    """register_browser_tools should populate a ToolRegistry with proxy ToolSpecs."""
    from obscura.core.tools import ToolRegistry

    from obscura.integrations.browser.client import register_browser_tools

    sock = short_tmp / "bridge.sock"
    reg = short_tmp / "active.json"

    async def call(name: str, args: dict[str, Any]) -> Any:
        return {"name": name, "args": args}

    bridge = SocketBridge(path=sock, tools_provider=_stub_specs, call=call)
    await bridge.start()
    try:
        active_hosts.register(
            pid=os.getpid(), socket=sock, version="0.1.0", path=reg
        )

        # Patch discovery to use our temp registry.
        import obscura.integrations.browser.active_hosts as ah

        original = ah.default_registry_path
        ah.default_registry_path = lambda: reg
        try:
            tool_registry = ToolRegistry()
            client = await register_browser_tools(tool_registry.register)
            try:
                # The proxy tool should be in the registry under its public name.
                spec = tool_registry.get("browser_read_page")
                assert spec is not None
                # Calling the proxy handler routes through the bridge.
                result = await spec.handler(max_chars=99)
                assert result == {
                    "name": "browser_read_page",
                    "args": {"max_chars": 99},
                }
            finally:
                await client.close()
        finally:
            ah.default_registry_path = original
            active_hosts.unregister(pid=os.getpid(), path=reg)
    finally:
        await bridge.stop()
