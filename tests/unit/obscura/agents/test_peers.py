"""Tests for local peer discovery and invocation."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from obscura.agent.agents import Agent, AgentRuntime
from obscura.agent.peers import AgentRef, RemoteAgentRef
from obscura.auth.models import AuthenticatedUser


@pytest.fixture
def test_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="u-peers-1",
        email="peers@obscura.dev",
        roles=("admin",),
        org_id="org-peers",
        token_type="user",
        raw_token="token",
    )


@pytest.fixture
def runtime(test_user: AuthenticatedUser) -> AgentRuntime:
    return AgentRuntime(user=test_user)


def _spawn(runtime: AgentRuntime, name: str) -> Agent:
    with patch.dict(os.environ, {"OBSCURA_HEARTBEAT_ENABLED": "false"}):
        return runtime.spawn(name=name, model="claude")


class TestPeerRegistry:
    def test_discover_local_refs(self, runtime: AgentRuntime) -> None:
        a = _spawn(runtime, "a")
        b = _spawn(runtime, "b")
        refs = runtime.peer_registry.discover()
        ids = {ref.agent_id for ref in refs}
        assert a.id in ids
        assert b.id in ids
        assert all(ref.kind == "local" for ref in refs)

    @pytest.mark.asyncio
    async def test_runtime_discover_peers_merges_local_and_remote(
        self, runtime: AgentRuntime
    ) -> None:
        source = _spawn(runtime, "source")
        target = _spawn(runtime, "target")
        source.config.a2a_remote_tools = {
            "enabled": True,
            "urls": ["https://a2a.one", "https://a2a.two"],
            "auth_token": "token",
        }

        with patch.object(
            runtime.peer_registry,
            "discover_remote",
            AsyncMock(
                return_value=[
                    RemoteAgentRef(url="https://a2a.one"),
                    RemoteAgentRef(url="https://a2a.two"),
                ]
            ),
        ) as mocked_remote:
            catalog = await runtime.discover_peers_for_agent(
                source.id,
                include_self=False,
                discover_remote=False,
            )

        local_ids = {ref.agent_id for ref in catalog.local}
        assert source.id not in local_ids
        assert target.id in local_ids
        assert len(catalog.remote) == 2
        mocked_remote.assert_awaited_once()


class TestPeerInvoke:
    @pytest.mark.asyncio
    async def test_invoke_peer_requires_feature_flag(self, runtime: AgentRuntime) -> None:
        target = _spawn(runtime, "target")
        with pytest.raises(RuntimeError, match="OBSCURA_PEER_CALLS_ENABLED"):
            await runtime.invoke_peer(target.id, "hello")

    @pytest.mark.asyncio
    async def test_invoke_peer_blocking_with_envelope(self, runtime: AgentRuntime) -> None:
        source = _spawn(runtime, "source")
        target = _spawn(runtime, "target")
        target.run = AsyncMock(return_value="peer-ok")
        with patch.dict(os.environ, {"OBSCURA_PEER_CALLS_ENABLED": "true"}):
            result = await source.invoke_peer(target.id, "hello peer", custom="x")

        assert result == "peer-ok"
        target.run.assert_awaited_once()
        called_prompt = target.run.await_args.args[0]  # type: ignore[attr-defined]
        called_context = target.run.await_args.kwargs  # type: ignore[attr-defined]
        assert called_prompt == "hello peer"
        assert called_context["custom"] == "x"
        envelope = called_context["_peer_request"]
        assert envelope["caller_agent_id"] == source.id
        assert envelope["target_agent_id"] == target.id
        assert envelope["mode"] == "blocking"
        assert isinstance(envelope["request_id"], str)

    @pytest.mark.asyncio
    async def test_stream_peer_with_ref(self, runtime: AgentRuntime) -> None:
        source = _spawn(runtime, "source")
        target = _spawn(runtime, "target")
        seen: dict[str, Any] = {}

        async def _fake_stream(prompt: str, **context: Any) -> AsyncIterator[str]:
            seen["prompt"] = prompt
            seen["context"] = context
            yield "chunk-1"
            yield "chunk-2"

        target.stream = _fake_stream
        with patch.dict(os.environ, {"OBSCURA_PEER_CALLS_ENABLED": "true"}):
            ref = AgentRef(
                runtime_id=runtime.runtime_id,
                agent_id=target.id,
                name=target.config.name,
                model=target.config.model,
                status=target.status.name,
            )
            chunks: list[str] = []
            async for chunk in source.stream_peer(ref, "stream please"):
                chunks.append(chunk)

        assert chunks == ["chunk-1", "chunk-2"]
        assert seen["prompt"] == "stream please"
        envelope = seen["context"]["_peer_request"]
        assert envelope["caller_agent_id"] == source.id
        assert envelope["target_agent_id"] == target.id
        assert envelope["mode"] == "streaming"
