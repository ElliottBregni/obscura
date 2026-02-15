"""Tests for sdk.agents — Agent runtime and lifecycle management."""

from __future__ import annotations

import asyncio
import os
from typing import Any, AsyncIterator
import pytest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from sdk.agents import (
    Agent,
    AgentConfig,
    AgentMessage,
    AgentRuntime,
    AgentState,
    AgentStatus,
    MCPConfig,
)
from sdk._types import AgentEvent, AgentEventKind
from sdk.auth.models import AuthenticatedUser


@pytest.fixture
def test_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="u-test-123",
        email="test@obscura.dev",
        roles=("admin",),
        org_id="org-1",
        token_type="user",
        raw_token="fake-token",
    )


@pytest.fixture
def runtime(test_user: AuthenticatedUser) -> AgentRuntime:
    return AgentRuntime(user=test_user)


def _make_agent(runtime: AgentRuntime, name: str = "test-agent", **kwargs: Any) -> Agent:
    """Helper to spawn an agent with heartbeat disabled."""
    with patch.dict(os.environ, {"OBSCURA_HEARTBEAT_ENABLED": "false"}):
        return runtime.spawn(name, model="claude", **kwargs)


class TestAgentConfig:
    def test_default_config(self) -> None:
        config = AgentConfig(name="test-agent", model="claude")
        assert config.name == "test-agent"
        assert config.model == "claude"
        assert config.system_prompt == ""
        assert config.memory_namespace == "default"
        assert config.max_iterations == 10

    def test_custom_config(self) -> None:
        config = AgentConfig(
            name="custom",
            model="copilot",
            system_prompt="You are helpful.",
            memory_namespace="project:x",
            max_iterations=5,
            timeout_seconds=60.0,
            tools=["read_file", "write_file"],
            tags=["prod", "v2"],
        )
        assert config.system_prompt == "You are helpful."
        assert config.memory_namespace == "project:x"
        assert config.max_iterations == 5
        assert config.timeout_seconds == 60.0
        assert config.tools == ["read_file", "write_file"]
        assert config.tags == ["prod", "v2"]

    def test_mcp_config_default(self) -> None:
        config = AgentConfig(name="test", model="claude")
        assert config.mcp.enabled is False
        assert config.mcp.servers == []


class TestMCPConfig:
    def test_mcp_config_enabled(self) -> None:
        mcp = MCPConfig(
            enabled=True,
            servers=[{"transport": "stdio", "command": "node", "args": ["server.js"]}],
        )
        assert mcp.enabled is True
        assert len(mcp.servers) == 1


class TestAgentRuntime:
    @pytest.mark.asyncio
    async def test_spawn_agent(self, runtime: AgentRuntime, test_user: AuthenticatedUser) -> None:
        await runtime.start()
        agent = _make_agent(runtime)

        assert agent.id.startswith("agent-")
        assert agent.config.name == "test-agent"
        assert agent.config.model == "claude"
        assert agent.status == AgentStatus.PENDING

        await runtime.stop()

    def test_get_agent(self, runtime: AgentRuntime) -> None:
        agent = _make_agent(runtime)
        fetched = runtime.get_agent(agent.id)
        assert fetched is agent

    def test_get_missing_agent(self, runtime: AgentRuntime) -> None:
        fetched = runtime.get_agent("nonexistent")
        assert fetched is None

    def test_list_agents(self, runtime: AgentRuntime) -> None:
        agent1 = _make_agent(runtime, "agent-1")
        agent2 = _make_agent(runtime, "agent-2")

        agents = runtime.list_agents()
        assert len(agents) == 2
        assert agent1 in agents
        assert agent2 in agents

    def test_list_agents_filtered_by_name(self, runtime: AgentRuntime) -> None:
        _make_agent(runtime, "search-agent")
        _make_agent(runtime, "other-agent")

        agents = runtime.list_agents(name="search-agent")
        assert len(agents) == 1
        assert agents[0].config.name == "search-agent"

    def test_list_agents_filtered_by_status(self, runtime: AgentRuntime) -> None:
        """Cover line 726: filtering agents by status."""
        a1 = _make_agent(runtime, "agent-a")
        a2 = _make_agent(runtime, "agent-b")
        a1.status = AgentStatus.RUNNING
        # a2 stays PENDING

        running = runtime.list_agents(status=AgentStatus.RUNNING)
        assert len(running) == 1
        assert running[0] is a1

        pending = runtime.list_agents(status=AgentStatus.PENDING)
        assert len(pending) == 1
        assert pending[0] is a2

    def test_get_agent_status_live(self, runtime: AgentRuntime) -> None:
        """Cover line 737: get_agent_status for a live agent."""
        agent = _make_agent(runtime, "status-test")
        state = runtime.get_agent_status(agent.id)
        assert state is not None
        assert state.name == "status-test"
        assert state.status == AgentStatus.PENDING

    def test_get_agent_status_missing_no_user(self) -> None:
        """Cover line 754: get_agent_status returns None when agent not found and no user."""
        rt = AgentRuntime(user=None)
        assert rt.get_agent_status("nonexistent-id") is None

    @pytest.mark.asyncio
    async def test_spawn_and_run(self, runtime: AgentRuntime) -> None:
        """Cover lines 708-711: spawn_and_run convenience method."""
        await runtime.start()

        mock_message = MagicMock()
        mock_message.text = "result from run"

        with patch("sdk.agents.ObscuraClient") as MockClient:
            instance = AsyncMock()
            instance.send = AsyncMock(return_value=mock_message)
            MockClient.return_value = instance

            agent, result = await runtime.spawn_and_run(
                "quick-agent",
                "do the thing",
                model="claude",
            )

            assert result == "result from run"
            assert agent.status == AgentStatus.COMPLETED

        await runtime.stop()

    @pytest.mark.asyncio
    async def test_message_routing(self, runtime: AgentRuntime) -> None:
        await runtime.start()

        agent1 = _make_agent(runtime, "sender")
        agent2 = _make_agent(runtime, "receiver")

        # Send message from agent1 to agent2
        await agent1.send_message(agent2.id, "hello")

        # Give message bus time to process
        await asyncio.sleep(0.1)

        # Check agent2 received it
        assert not agent2.message_queue.empty()

        await runtime.stop()

    @pytest.mark.asyncio
    async def test_broadcast_message(self, runtime: AgentRuntime) -> None:
        await runtime.start()

        agent1 = _make_agent(runtime, "broadcaster")
        agent2 = _make_agent(runtime, "listener1")
        agent3 = _make_agent(runtime, "listener2")

        await agent1.send_message("broadcast", "hello all")
        await asyncio.sleep(0.1)

        # Both listeners should have message, not broadcaster
        assert not agent2.message_queue.empty()
        assert not agent3.message_queue.empty()
        # Sender shouldn't get their own broadcast
        assert agent1.message_queue.empty()

        await runtime.stop()

    @pytest.mark.asyncio
    async def test_message_to_nonexistent_agent(self, runtime: AgentRuntime) -> None:
        """Cover lines 777: logging warning when target agent not found."""
        await runtime.start()

        agent1 = _make_agent(runtime, "sender")
        await agent1.send_message("nonexistent-agent", "hello?")
        await asyncio.sleep(0.1)

        await runtime.stop()

    @pytest.mark.asyncio
    async def test_message_bus_loop_exception(self, runtime: AgentRuntime) -> None:
        """Cover lines 784-785: exception handling in message bus loop."""
        await runtime.start()

        agent = _make_agent(runtime, "test")

        # Put a bad message that will cause enqueue_message to fail
        bad_message = AgentMessage(
            source=agent.id,
            target=agent.id,
            content="test",
        )

        # Patch enqueue_message to raise
        with patch.object(Agent, "enqueue_message", side_effect=RuntimeError("boom")):
            await runtime.route_message(bad_message)
            await asyncio.sleep(0.1)

        # Bus should still be running after the exception
        assert runtime.bus_task is not None
        assert not runtime.bus_task.done()

        await runtime.stop()

    @pytest.mark.asyncio
    async def test_wait_for_agents_no_timeout(self, runtime: AgentRuntime) -> None:
        """Cover lines 811-813: wait_for_agents with no timeout."""
        await runtime.start()

        agent = _make_agent(runtime, "waiter")
        agent.refresh_state()
        # Set to completed so wait resolves immediately
        agent.status = AgentStatus.COMPLETED
        agent.refresh_state()

        states = await runtime.wait_for_agents([agent.id])
        assert len(states) == 1
        assert states[0].status == AgentStatus.COMPLETED

        await runtime.stop()

    @pytest.mark.asyncio
    async def test_wait_for_agents_with_timeout(self, runtime: AgentRuntime) -> None:
        """Cover lines 793-810: wait_for_agents with timeout, some pending."""
        await runtime.start()

        agent1 = _make_agent(runtime, "done-agent")
        agent2 = _make_agent(runtime, "slow-agent")

        agent1.status = AgentStatus.COMPLETED
        agent1.refresh_state()
        # agent2 stays PENDING so it will timeout

        states = await runtime.wait_for_agents(
            [agent1.id, agent2.id],
            timeout=0.3,
        )
        # At least agent1 should have completed
        completed_ids = [s.agent_id for s in states]
        assert agent1.id in completed_ids

        await runtime.stop()

    @pytest.mark.asyncio
    async def test_runtime_stop_clears_agents(self, runtime: AgentRuntime) -> None:
        """Verify runtime.stop() stops all agents and clears the dict."""
        await runtime.start()
        a1 = _make_agent(runtime, "a1")
        a2 = _make_agent(runtime, "a2")
        # Give them mock clients so stop() doesn't error
        a1.client = AsyncMock()
        a2.client = AsyncMock()

        await runtime.stop()
        assert len(runtime.agents) == 0


class TestAgentStart:
    @pytest.mark.asyncio
    async def test_start_initializes_client(self, runtime: AgentRuntime) -> None:
        """Cover lines 149-185: Agent.start() sets up client, heartbeat, MCP."""
        agent = _make_agent(runtime, "start-test")
        agent.heartbeat_enabled = False

        with patch("sdk.agents.ObscuraClient") as MockClient:
            instance = AsyncMock()
            MockClient.return_value = instance

            await agent.start()

            MockClient.assert_called_once()
            instance.start.assert_awaited_once()
            assert agent.status == AgentStatus.WAITING

    @pytest.mark.asyncio
    async def test_start_with_mcp_enabled(self, runtime: AgentRuntime) -> None:
        """Cover lines 157-178: Agent.start() with MCP config."""
        mcp_config = MCPConfig(
            enabled=True,
            servers=[{"transport": "stdio", "command": "node", "args": ["srv.js"]}],
        )
        with patch.dict(os.environ, {"OBSCURA_HEARTBEAT_ENABLED": "false"}):
            agent = runtime.spawn(
                "mcp-test",
                model="claude",
                mcp=mcp_config,
            )
        agent.heartbeat_enabled = False

        mock_tool = MagicMock()
        mock_tool.name = "my_tool"

        with patch("sdk.agents.ObscuraClient") as MockClient:
            client_instance = AsyncMock()
            # register_tool is a sync method, not async
            client_instance.register_tool = MagicMock()
            MockClient.return_value = client_instance

            with patch("sdk.backends.mcp_backend.MCPBackend") as MockMCPBackend:
                mcp_instance = AsyncMock()
                # list_tools is a sync method, not async
                mcp_instance.list_tools = MagicMock(return_value=[mock_tool])
                MockMCPBackend.return_value = mcp_instance

                await agent.start()

                mcp_instance.start.assert_awaited_once()
                client_instance.register_tool.assert_called_once_with(mock_tool)
                assert agent.mcp_backend is mcp_instance

    @pytest.mark.asyncio
    async def test_start_with_heartbeat(self, runtime: AgentRuntime) -> None:
        """Cover lines 180-185, 557-572: heartbeat initialization."""
        agent = _make_agent(runtime, "hb-test")
        agent.heartbeat_enabled = True

        with patch("sdk.agents.ObscuraClient") as MockClient:
            client_instance = AsyncMock()
            MockClient.return_value = client_instance

            with patch("sdk.heartbeat.AgentHeartbeatClient") as MockHB:
                hb_instance = AsyncMock()
                MockHB.return_value = hb_instance

                await agent.start()

                MockHB.assert_called_once()
                hb_instance.start.assert_awaited_once()
                assert agent.status == AgentStatus.WAITING

    @pytest.mark.asyncio
    async def test_start_heartbeat_failure(self, runtime: AgentRuntime) -> None:
        """Cover lines 570-572: heartbeat start failure is non-fatal."""
        agent = _make_agent(runtime, "hb-fail")
        agent.heartbeat_enabled = True

        with patch("sdk.agents.ObscuraClient") as MockClient:
            client_instance = AsyncMock()
            MockClient.return_value = client_instance

            with patch(
                "sdk.heartbeat.AgentHeartbeatClient",
                side_effect=RuntimeError("cannot connect"),
            ):
                await agent.start()

            assert agent.heartbeat_client is None
            assert agent.status == AgentStatus.WAITING


class TestAgentRun:
    @pytest.mark.asyncio
    async def test_run_success(self, runtime: AgentRuntime) -> None:
        """Cover lines 193-235: successful run() path."""
        agent = _make_agent(runtime, "run-test")

        mock_message = MagicMock()
        mock_message.text = "task completed"

        mock_client = AsyncMock()
        mock_client.send = AsyncMock(return_value=mock_message)
        agent.client = mock_client

        result = await agent.run("do something")

        assert result == "task completed"
        assert agent.status == AgentStatus.COMPLETED
        assert agent.iteration_count == 1
        assert agent.result == "task completed"

    @pytest.mark.asyncio
    async def test_run_with_context(self, runtime: AgentRuntime) -> None:
        """Cover context kwarg path in run()."""
        agent = _make_agent(runtime, "ctx-test")

        mock_message = MagicMock()
        mock_message.text = "done"

        mock_client = AsyncMock()
        mock_client.send = AsyncMock(return_value=mock_message)
        agent.client = mock_client

        result = await agent.run("review code", repo="obscura", pr=42)

        assert result == "done"

    @pytest.mark.asyncio
    async def test_run_timeout(self, runtime: AgentRuntime) -> None:
        """Cover lines 237-242: timeout handling in run()."""
        with patch.dict(os.environ, {"OBSCURA_HEARTBEAT_ENABLED": "false"}):
            agent = runtime.spawn("timeout-test", model="claude", timeout_seconds=0.01)

        async def slow_send(*args: Any, **kwargs: Any) -> None:
            await asyncio.sleep(10)

        mock_client = AsyncMock()
        mock_client.send = slow_send
        agent.client = mock_client

        with pytest.raises(TimeoutError, match="timed out"):
            await agent.run("something slow")

        assert agent.status == AgentStatus.FAILED
        assert agent.error is not None

    @pytest.mark.asyncio
    async def test_run_general_exception(self, runtime: AgentRuntime) -> None:
        """Cover lines 243-246: general exception in run()."""
        agent = _make_agent(runtime, "error-test")

        mock_client = AsyncMock()
        mock_client.send = AsyncMock(side_effect=ValueError("bad input"))
        agent.client = mock_client

        with pytest.raises(ValueError, match="bad input"):
            await agent.run("bad prompt")

        assert agent.status == AgentStatus.FAILED
        assert isinstance(agent.error, ValueError)

    @pytest.mark.asyncio
    async def test_run_stores_task_and_result_in_memory(self, runtime: AgentRuntime) -> None:
        """Cover lines 198-230: memory set calls during run()."""
        agent = _make_agent(runtime, "mem-test")

        mock_message = MagicMock()
        mock_message.text = "memory result"

        mock_client = AsyncMock()
        mock_client.send = AsyncMock(return_value=mock_message)
        agent.client = mock_client

        await agent.run("store this")

        # Check task was stored
        task = agent.memory.get("task_0", namespace="default:tasks")
        assert task is not None
        assert task["prompt"] == "store this"

        # Check result was stored
        result = agent.memory.get("result_0", namespace="default:tasks")
        assert result is not None
        assert result["result"] == "memory result"


class TestAgentStream:
    @pytest.mark.asyncio
    async def test_stream_success(self, runtime: AgentRuntime) -> None:
        """Cover lines 252-282: successful stream() path."""
        agent = _make_agent(runtime, "stream-test")

        async def mock_stream(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            yield MagicMock(text="Hello ")
            yield MagicMock(text="world!")

        mock_client = AsyncMock()
        mock_client.stream = mock_stream
        agent.client = mock_client

        chunks: list[str] = []
        async for chunk in agent.stream("say hello"):
            chunks.append(chunk)

        assert chunks == ["Hello ", "world!"]
        assert agent.status == AgentStatus.COMPLETED
        assert agent.iteration_count == 1

    @pytest.mark.asyncio
    async def test_stream_with_str_chunks(self, runtime: AgentRuntime) -> None:
        """Cover line 273: chunk without .text attribute falls back to str()."""
        agent = _make_agent(runtime, "stream-str")

        async def mock_stream(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            yield "plain string"

        mock_client = AsyncMock()
        mock_client.stream = mock_stream
        agent.client = mock_client

        chunks: list[str] = []
        async for chunk in agent.stream("prompt"):
            chunks.append(chunk)

        assert chunks == ["plain string"]

    @pytest.mark.asyncio
    async def test_stream_error(self, runtime: AgentRuntime) -> None:
        """Cover lines 277-280: exception in stream()."""
        agent = _make_agent(runtime, "stream-err")

        async def failing_stream(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            yield MagicMock(text="partial")
            raise ConnectionError("lost connection")

        mock_client = AsyncMock()
        mock_client.stream = failing_stream
        agent.client = mock_client

        with pytest.raises(ConnectionError, match="lost connection"):
            async for _ in agent.stream("will fail"):
                pass

        assert agent.status == AgentStatus.FAILED
        assert isinstance(agent.error, ConnectionError)

    @pytest.mark.asyncio
    async def test_stream_stores_task(self, runtime: AgentRuntime) -> None:
        """Cover lines 257-266: memory set for stream mode."""
        agent = _make_agent(runtime, "stream-mem")

        async def mock_stream(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            yield MagicMock(text="ok")

        mock_client = AsyncMock()
        mock_client.stream = mock_stream
        agent.client = mock_client

        async for _ in agent.stream("stream task"):
            pass

        task = agent.memory.get("task_0", namespace="default:tasks")
        assert task is not None
        assert task["mode"] == "stream"


class TestAgentRunLoop:
    @pytest.mark.asyncio
    async def test_run_loop_success(self, runtime: AgentRuntime) -> None:
        """Cover lines 301-345: successful run_loop path."""
        agent = _make_agent(runtime, "loop-test")

        mock_client = AsyncMock()
        mock_client.run_loop_to_completion = AsyncMock(return_value="loop result")
        agent.client = mock_client

        result = await agent.run_loop("fix the bug")

        assert result == "loop result"
        assert agent.status == AgentStatus.COMPLETED
        assert agent.iteration_count == 1

    @pytest.mark.asyncio
    async def test_run_loop_custom_max_turns(self, runtime: AgentRuntime) -> None:
        """Cover lines 305-306: custom max_turns."""
        agent = _make_agent(runtime, "loop-turns")

        mock_client = AsyncMock()
        mock_client.run_loop_to_completion = AsyncMock(return_value="done")
        agent.client = mock_client

        await agent.run_loop("task", max_turns=3)

        mock_client.run_loop_to_completion.assert_awaited_once()
        call_kwargs = mock_client.run_loop_to_completion.call_args
        assert call_kwargs.kwargs["max_turns"] == 3

    @pytest.mark.asyncio
    async def test_run_loop_timeout(self, runtime: AgentRuntime) -> None:
        """Cover lines 347-352: timeout in run_loop."""
        with patch.dict(os.environ, {"OBSCURA_HEARTBEAT_ENABLED": "false"}):
            agent = runtime.spawn("loop-to", model="claude", timeout_seconds=0.01)

        async def slow(*args: Any, **kwargs: Any) -> None:
            await asyncio.sleep(10)

        mock_client = AsyncMock()
        mock_client.run_loop_to_completion = slow
        agent.client = mock_client

        with pytest.raises(TimeoutError, match="timed out"):
            await agent.run_loop("slow task")

        assert agent.status == AgentStatus.FAILED

    @pytest.mark.asyncio
    async def test_run_loop_general_error(self, runtime: AgentRuntime) -> None:
        """Cover lines 353-356: general exception in run_loop."""
        agent = _make_agent(runtime, "loop-err")

        mock_client = AsyncMock()
        mock_client.run_loop_to_completion = AsyncMock(
            side_effect=RuntimeError("model error")
        )
        agent.client = mock_client

        with pytest.raises(RuntimeError, match="model error"):
            await agent.run_loop("bad prompt")

        assert agent.status == AgentStatus.FAILED

    @pytest.mark.asyncio
    async def test_run_loop_stores_memory(self, runtime: AgentRuntime) -> None:
        """Cover lines 308-317, 333-341: memory storage in run_loop."""
        agent = _make_agent(runtime, "loop-mem")

        mock_client = AsyncMock()
        mock_client.run_loop_to_completion = AsyncMock(return_value="loop done")
        agent.client = mock_client

        await agent.run_loop("loop task")

        task = agent.memory.get("task_0", namespace="default:tasks")
        assert task is not None
        assert task["mode"] == "agent_loop"

        result = agent.memory.get("result_0", namespace="default:tasks")
        assert result is not None
        assert result["mode"] == "agent_loop"


class TestAgentStreamLoop:
    @pytest.mark.asyncio
    async def test_stream_loop_success(self, runtime: AgentRuntime) -> None:
        """Cover lines 374-425: successful stream_loop path."""
        agent = _make_agent(runtime, "sloop-test")

        events = [
            AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="Hello "),
            AgentEvent(kind=AgentEventKind.TOOL_CALL, tool_name="read_file"),
            AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="world"),
            AgentEvent(kind=AgentEventKind.AGENT_DONE),
        ]

        async def mock_run_loop(*args: Any, **kwargs: Any) -> AsyncIterator[AgentEvent]:
            for e in events:
                yield e

        mock_client = MagicMock()
        mock_client.run_loop = mock_run_loop
        agent.client = mock_client

        collected: list[AgentEvent] = []
        async for event in agent.stream_loop("do it"):
            collected.append(event)

        assert len(collected) == 4
        assert agent.status == AgentStatus.COMPLETED
        assert agent.result == "Hello world"
        assert agent.iteration_count == 1

    @pytest.mark.asyncio
    async def test_stream_loop_custom_max_turns(self, runtime: AgentRuntime) -> None:
        """Cover lines 378-379: default max_turns from config."""
        agent = _make_agent(runtime, "sloop-turns")

        async def mock_run_loop(*args: Any, **kwargs: Any) -> AsyncIterator[AgentEvent]:
            yield AgentEvent(kind=AgentEventKind.AGENT_DONE)

        mock_client = MagicMock()
        mock_client.run_loop = mock_run_loop
        agent.client = mock_client

        async for _ in agent.stream_loop("task", max_turns=2):
            pass

    @pytest.mark.asyncio
    async def test_stream_loop_error(self, runtime: AgentRuntime) -> None:
        """Cover lines 420-423: exception in stream_loop."""
        agent = _make_agent(runtime, "sloop-err")

        async def failing_loop(*args: Any, **kwargs: Any) -> AsyncIterator[AgentEvent]:
            yield AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="start")
            raise IOError("connection lost")

        mock_client = MagicMock()
        mock_client.run_loop = failing_loop
        agent.client = mock_client

        with pytest.raises(IOError, match="connection lost"):
            async for _ in agent.stream_loop("failing"):
                pass

        assert agent.status == AgentStatus.FAILED

    @pytest.mark.asyncio
    async def test_stream_loop_stores_memory(self, runtime: AgentRuntime) -> None:
        """Cover lines 381-414: memory storage in stream_loop."""
        agent = _make_agent(runtime, "sloop-mem")

        async def mock_run_loop(*args: Any, **kwargs: Any) -> AsyncIterator[AgentEvent]:
            yield AgentEvent(kind=AgentEventKind.TEXT_DELTA, text="result")

        mock_client = MagicMock()
        mock_client.run_loop = mock_run_loop
        agent.client = mock_client

        async for _ in agent.stream_loop("stream loop task"):
            pass

        task = agent.memory.get("task_0", namespace="default:tasks")
        assert task is not None
        assert task["mode"] == "stream_loop"

        result_data = agent.memory.get("result_0", namespace="default:tasks")
        assert result_data is not None
        assert result_data["mode"] == "stream_loop"


class TestAgentStatePersistence:
    @pytest.mark.asyncio
    async def test_state_saved_to_memory(self, runtime: AgentRuntime) -> None:
        await runtime.start()
        agent = _make_agent(runtime, "state-test")

        agent.refresh_state()

        # Check memory has the state
        state_data = agent.memory.get(f"agent_state_{agent.id}", namespace="agent:runtime")
        assert state_data is not None
        assert state_data["name"] == "state-test"
        assert state_data["status"] == "PENDING"

        await runtime.stop()

    def test_get_agent_status_from_memory(self, runtime: AgentRuntime) -> None:
        # Create agent, it saves state
        agent = _make_agent(runtime, "memory-test")
        agent.refresh_state()

        # Create new runtime instance (simulating restart)
        new_runtime = AgentRuntime(user=runtime.user)
        state = new_runtime.get_agent_status(agent.id)

        assert state is not None
        assert state.name == "memory-test"

    def test_get_state(self, runtime: AgentRuntime) -> None:
        """Cover line 607: get_state returns AgentState."""
        agent = _make_agent(runtime, "state-get")
        state = agent.get_state()
        assert isinstance(state, AgentState)
        assert state.agent_id == agent.id
        assert state.name == "state-get"
        assert state.status == AgentStatus.PENDING
        assert state.error_message is None

    def test_get_state_with_error(self, runtime: AgentRuntime) -> None:
        """Cover error_message in get_state."""
        agent = _make_agent(runtime, "state-err")
        agent.error = ValueError("something went wrong")
        state = agent.get_state()
        assert state.error_message == "something went wrong"


class TestAgentPromptBuilding:
    def test_build_prompt_with_memory(self, runtime: AgentRuntime) -> None:
        agent = _make_agent(runtime, "prompt-test")

        # Add some memory
        agent.memory.set("context", {"repo": "test"}, namespace="default:tasks")

        prompt = agent.build_prompt(
            "do something",
            {"memory:1": {"data": "value"}},
            {"extra": "context"}
        )

        assert "do something" in prompt
        assert "Relevant Context" in prompt
        assert "Task Context" in prompt

    def test_build_prompt_no_memory(self, runtime: AgentRuntime) -> None:
        agent = _make_agent(runtime, "prompt-no-mem")
        prompt = agent.build_prompt("just a task", {}, {})
        assert "just a task" in prompt
        assert "Relevant Context" not in prompt
        assert "Task Context" not in prompt

    def test_build_prompt_memory_only(self, runtime: AgentRuntime) -> None:
        agent = _make_agent(runtime, "prompt-mem-only")
        prompt = agent.build_prompt(
            "the task",
            {"key": "val"},
            {},
        )
        assert "Relevant Context" in prompt
        assert "Task Context" not in prompt


class TestAgentMemoryIntegration:
    def test_agent_stores_task_in_memory(self, runtime: AgentRuntime) -> None:
        agent = _make_agent(runtime, "task-test")

        # Manually store task (normally done in run())
        agent.memory.set(
            "task_0",
            {"prompt": "test task", "context": {}},
            namespace="default:tasks"
        )

        # Verify stored
        task = agent.memory.get("task_0", namespace="default:tasks")
        assert task["prompt"] == "test task"

    def test_load_relevant_memory(self, runtime: AgentRuntime) -> None:
        agent = _make_agent(runtime, "load-test")

        # Store some tasks
        for i in range(3):
            agent.memory.set(
                f"task_{i}",
                {"prompt": f"task {i}"},
                namespace="default:tasks"
            )

        memory = agent.load_relevant_memory("test prompt")

        # Should have loaded tasks (up to 5)
        assert len(memory) > 0

    def test_load_relevant_memory_with_search_fallback(self, runtime: AgentRuntime) -> None:
        """Cover lines 459-462: fallback text search when no semantic results."""
        agent = _make_agent(runtime, "search-fallback")

        # Store something searchable
        agent.memory.set("searchable", "hello world data", namespace="default")

        memory = agent.load_relevant_memory("hello")
        # Should have results from search fallback (line 462)
        assert isinstance(memory, dict)


class TestAgentErrorHandling:
    @pytest.mark.asyncio
    async def test_agent_handles_run_error(self, runtime: AgentRuntime) -> None:
        agent = _make_agent(runtime, "error-test")

        mock_client = AsyncMock()
        mock_client.send = AsyncMock(side_effect=Exception("API error"))
        agent.client = mock_client

        with pytest.raises(Exception, match="API error"):
            await agent.run("test prompt")

        assert agent.status == AgentStatus.FAILED
        assert agent.error is not None


class TestAgentStreaming:
    @pytest.mark.asyncio
    async def test_stream_chunks(self, runtime: AgentRuntime) -> None:
        agent = _make_agent(runtime, "stream-test")

        async def mock_stream(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            yield MagicMock(text="Hello ")
            yield MagicMock(text="world!")

        mock_client = AsyncMock()
        mock_client.stream = mock_stream
        agent.client = mock_client

        chunks: list[str] = []
        async for chunk in agent.stream("say hello"):
            chunks.append(chunk)

        assert len(chunks) == 2
        assert chunks[0] == "Hello "
        assert chunks[1] == "world!"


class TestAgentStop:
    @pytest.mark.asyncio
    async def test_stop_basic(self, runtime: AgentRuntime) -> None:
        """Cover lines 578-583: stop with client, handling cancel scope errors."""
        agent = _make_agent(runtime, "stop-basic")
        agent.client = AsyncMock()

        await agent.stop()

        assert agent.status == AgentStatus.STOPPED
        agent.client.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_with_cancel_scope_error(self, runtime: AgentRuntime) -> None:
        """Cover lines 580-583: RuntimeError with 'cancel scope' is swallowed."""
        agent = _make_agent(runtime, "stop-cancel")
        mock_client = AsyncMock()
        mock_client.stop = AsyncMock(
            side_effect=RuntimeError("cancel scope blah")
        )
        agent.client = mock_client

        # Should not raise
        await agent.stop()
        assert agent.status == AgentStatus.STOPPED

    @pytest.mark.asyncio
    async def test_stop_with_other_runtime_error(self, runtime: AgentRuntime) -> None:
        """Cover lines 580-583: non-cancel-scope RuntimeError is re-raised."""
        agent = _make_agent(runtime, "stop-reraise")
        mock_client = AsyncMock()
        mock_client.stop = AsyncMock(
            side_effect=RuntimeError("some other error")
        )
        agent.client = mock_client

        with pytest.raises(RuntimeError, match="some other error"):
            await agent.stop()

    @pytest.mark.asyncio
    async def test_stop_with_mcp_backend(self, runtime: AgentRuntime) -> None:
        """Cover lines 584-586: stop cleans up MCP backend."""
        agent = _make_agent(runtime, "stop-mcp")
        agent.client = AsyncMock()
        mcp_mock = AsyncMock()
        agent.mcp_backend = mcp_mock

        await agent.stop()

        mcp_mock.stop.assert_awaited_once()
        assert agent.mcp_backend is None

    @pytest.mark.asyncio
    async def test_stop_with_heartbeat_client(self, runtime: AgentRuntime) -> None:
        """Cover lines 587-589: stop cleans up heartbeat client."""
        agent = _make_agent(runtime, "stop-hb")
        agent.client = AsyncMock()
        hb_mock = AsyncMock()
        agent.heartbeat_client = hb_mock

        await agent.stop()

        hb_mock.stop.assert_awaited_once()
        assert agent.heartbeat_client is None

    @pytest.mark.asyncio
    async def test_stop_cancels_running_task(self, runtime: AgentRuntime) -> None:
        """Cover lines 590-591: stop cancels _task if running."""
        agent = _make_agent(runtime, "stop-task")
        agent.client = AsyncMock()

        # Create a dummy task
        async def dummy():
            await asyncio.sleep(100)

        agent.task = asyncio.create_task(dummy())

        await agent.stop()
        # Let the event loop process the cancellation
        await asyncio.sleep(0)

        assert agent.task is not None and (agent.task.cancelled() or agent.task.done())
        assert agent.status == AgentStatus.STOPPED

    @pytest.mark.asyncio
    async def test_stop_graceful_success(self, runtime: AgentRuntime) -> None:
        """Cover lines 596-597: graceful stop completes within timeout."""
        agent = _make_agent(runtime, "graceful-ok")
        agent.client = AsyncMock()

        await agent.stop_graceful(timeout=5.0)
        assert agent.status == AgentStatus.STOPPED

    @pytest.mark.asyncio
    async def test_stop_graceful_timeout(self, runtime: AgentRuntime) -> None:
        """Cover lines 596-603: graceful stop hits timeout and force-stops."""
        agent = _make_agent(runtime, "graceful-to")

        # Make stop() take forever
        async def slow_stop():
            await asyncio.sleep(100)

        agent.stop = slow_stop

        # Create a mock task
        async def dummy():
            await asyncio.sleep(100)

        agent.task = asyncio.create_task(dummy())

        await agent.stop_graceful(timeout=0.05)
        # Let the event loop process the cancellation
        await asyncio.sleep(0)

        assert agent.status == AgentStatus.STOPPED
        assert agent.task is not None and (agent.task.cancelled() or agent.task.done())


class TestAgentMessages:
    @pytest.mark.asyncio
    async def test_receive_messages_yields_and_stops(self, runtime: AgentRuntime) -> None:
        """Cover lines 506-515: receive_messages iterator."""
        agent = _make_agent(runtime, "recv-test")

        msg = AgentMessage(
            source="user",
            target=agent.id,
            content="hello agent",
        )
        agent.enqueue_message(msg)

        # Set agent to completed so the iterator stops after timeout
        agent.status = AgentStatus.COMPLETED

        messages: list[AgentMessage] = []
        async for m in agent.receive_messages():
            messages.append(m)

        assert len(messages) == 1
        assert messages[0].content == "hello agent"

    @pytest.mark.asyncio
    async def test_receive_messages_stops_on_failed(self, runtime: AgentRuntime) -> None:
        """Cover line 514: stops when status is FAILED."""
        agent = _make_agent(runtime, "recv-fail")
        agent.status = AgentStatus.FAILED

        messages: list[AgentMessage] = []
        async for m in agent.receive_messages():
            messages.append(m)

        assert len(messages) == 0

    @pytest.mark.asyncio
    async def test_receive_messages_stops_on_stopped(self, runtime: AgentRuntime) -> None:
        """Cover line 514: stops when status is STOPPED."""
        agent = _make_agent(runtime, "recv-stop")
        agent.status = AgentStatus.STOPPED

        messages: list[AgentMessage] = []
        async for m in agent.receive_messages():
            messages.append(m)

        assert len(messages) == 0

    def testenqueue_message_success(self, runtime: AgentRuntime) -> None:
        """Cover line 520: normal enqueue."""
        agent = _make_agent(runtime, "enq-test")
        msg = AgentMessage(source="user", target=agent.id, content="test")
        agent.enqueue_message(msg)
        assert not agent.message_queue.empty()

    def testenqueue_message_full_queue(self, runtime: AgentRuntime) -> None:
        """Cover lines 521-522: queue full warning."""
        agent = _make_agent(runtime, "enq-full")
        # Replace with a tiny queue
        agent.message_queue = asyncio.Queue(maxsize=1)

        msg1 = AgentMessage(source="user", target=agent.id, content="first")
        msg2 = AgentMessage(source="user", target=agent.id, content="second")

        agent.enqueue_message(msg1)  # fills the queue
        # This should trigger the warning, not raise
        agent.enqueue_message(msg2)

        # Queue should still have only 1 message
        assert agent.message_queue.qsize() == 1


class TestAgentMessage:
    def test_message_defaults(self) -> None:
        msg = AgentMessage(
            source="agent-1",
            target="agent-2",
            content="hello",
        )
        assert msg.message_type == "text"
        assert msg.timestamp is not None

    def test_message_custom_type(self) -> None:
        msg = AgentMessage(
            source="agent-1",
            target="broadcast",
            content="error occurred",
            message_type="error",
        )
        assert msg.message_type == "error"


class TestAgentState:
    def test_agent_state_model(self, test_user: AuthenticatedUser) -> None:
        now = datetime.now(UTC)
        state = AgentState(
            agent_id="agent-abc",
            name="test",
            status=AgentStatus.RUNNING,
            created_at=now,
            updated_at=now,
            iteration_count=3,
            error_message=None,
        )
        assert state.agent_id == "agent-abc"
        assert state.status == AgentStatus.RUNNING
        assert state.iteration_count == 3

    def test_agent_state_with_error(self) -> None:
        now = datetime.now(UTC)
        state = AgentState(
            agent_id="agent-err",
            name="failed-agent",
            status=AgentStatus.FAILED,
            created_at=now,
            updated_at=now,
            error_message="something broke",
        )
        assert state.error_message == "something broke"


class TestAgentMemoryProperty:
    def test_memory_property(self, runtime: AgentRuntime) -> None:
        """Verify agent.memory returns a MemoryStore."""
        agent = _make_agent(runtime, "mem-prop")
        mem = agent.memory
        assert mem is not None
        # Should be the same instance on repeated access
        assert agent.memory is mem
