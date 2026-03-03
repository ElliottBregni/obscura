"""Tests for sdk.routes.websockets — broadcast_event, notify_memory_change, and helpers."""

import pytest
from unittest.mock import AsyncMock

from obscura.routes import websockets as ws_mod


class TestBroadcastEvent:
    @pytest.mark.asyncio
    async def test_broadcast_event_no_clients(self):
        original = ws_mod.broadcast_clients().copy()
        ws_mod.clear_broadcast_clients()
        try:
            await ws_mod.broadcast_event("test_event", {"key": "value"})
        finally:
            ws_mod.broadcast_clients().extend(original)

    @pytest.mark.asyncio
    async def test_broadcast_event_sends_to_clients(self):
        original = ws_mod.broadcast_clients().copy()
        ws_mod.clear_broadcast_clients()
        try:
            client1 = AsyncMock()
            client2 = AsyncMock()
            ws_mod.broadcast_clients().extend([client1, client2])

            await ws_mod.broadcast_event("agent_update", {"agent_id": "a1"})

            client1.send_json.assert_awaited_once()
            client2.send_json.assert_awaited_once()

            sent = client1.send_json.call_args[0][0]
            assert sent["type"] == "agent_update"
            assert sent["data"] == {"agent_id": "a1"}
            assert "timestamp" in sent
        finally:
            ws_mod.clear_broadcast_clients()
            ws_mod.broadcast_clients().extend(original)

    @pytest.mark.asyncio
    async def test_broadcast_event_removes_disconnected(self):
        original = ws_mod.broadcast_clients().copy()
        ws_mod.clear_broadcast_clients()
        try:
            good_client = AsyncMock()
            bad_client = AsyncMock()
            bad_client.send_json.side_effect = Exception("disconnected")
            ws_mod.broadcast_clients().extend([good_client, bad_client])

            await ws_mod.broadcast_event("test", {})

            good_client.send_json.assert_awaited_once()
            assert bad_client not in ws_mod.broadcast_clients()
            assert good_client in ws_mod.broadcast_clients()
        finally:
            ws_mod.clear_broadcast_clients()
            ws_mod.broadcast_clients().extend(original)


class TestNotifyMemoryChange:
    @pytest.mark.asyncio
    async def test_no_namespace(self):
        original = ws_mod.memory_watch_clients().copy()
        ws_mod.clear_memory_watch_clients()
        try:
            await ws_mod.notify_memory_change("missing", "set", "key1")
        finally:
            ws_mod.clear_memory_watch_clients()
            ws_mod.memory_watch_clients().update(original)

    @pytest.mark.asyncio
    async def test_notify_sends_to_watchers(self):
        original = ws_mod.memory_watch_clients().copy()
        ws_mod.clear_memory_watch_clients()
        try:
            client1 = AsyncMock()
            client2 = AsyncMock()
            ws_mod.memory_watch_clients()["ns1"] = [client1, client2]

            await ws_mod.notify_memory_change("ns1", "set", "my_key")

            client1.send_json.assert_awaited_once()
            client2.send_json.assert_awaited_once()

            sent = client1.send_json.call_args[0][0]
            assert sent["type"] == "set"
            assert sent["namespace"] == "ns1"
            assert sent["key"] == "my_key"
            assert "timestamp" in sent
        finally:
            ws_mod.clear_memory_watch_clients()
            ws_mod.memory_watch_clients().update(original)

    @pytest.mark.asyncio
    async def test_notify_removes_disconnected(self):
        original = ws_mod.memory_watch_clients().copy()
        ws_mod.clear_memory_watch_clients()
        try:
            good_client = AsyncMock()
            bad_client = AsyncMock()
            bad_client.send_json.side_effect = Exception("gone")
            ws_mod.memory_watch_clients()["ns2"] = [good_client, bad_client]

            await ws_mod.notify_memory_change("ns2", "delete", "k1")

            good_client.send_json.assert_awaited_once()
            assert bad_client not in ws_mod.memory_watch_clients()["ns2"]
            assert good_client in ws_mod.memory_watch_clients()["ns2"]
        finally:
            ws_mod.clear_memory_watch_clients()
            ws_mod.memory_watch_clients().update(original)

    @pytest.mark.asyncio
    async def test_notify_different_namespaces(self):
        original = ws_mod.memory_watch_clients().copy()
        ws_mod.clear_memory_watch_clients()
        try:
            ns1_client = AsyncMock()
            ns2_client = AsyncMock()
            ws_mod.memory_watch_clients()["ns1"] = [ns1_client]
            ws_mod.memory_watch_clients()["ns2"] = [ns2_client]

            await ws_mod.notify_memory_change("ns1", "set", "k1")

            ns1_client.send_json.assert_awaited_once()
            ns2_client.send_json.assert_not_awaited()
        finally:
            ws_mod.clear_memory_watch_clients()
            ws_mod.memory_watch_clients().update(original)
