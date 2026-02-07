"""Tests for sdk.agents — Agent runtime and lifecycle management."""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from sdk.agents import Agent, AgentConfig, AgentRuntime, AgentStatus
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


class TestAgentConfig:
    def test_default_config(self) -> None:
        config = AgentConfig(name="test-agent", model="claude")
        assert config.name == "test-agent"
        assert config.model == "claude"
        assert config.system_prompt == ""
        assert config.memory_namespace == "default"
        assert config.max_iterations == 10


class TestAgentRuntime:
    @pytest.mark.asyncio
    async def test_spawn_agent(self, runtime: AgentRuntime, test_user: AuthenticatedUser) -> None:
        await runtime.start()
        agent = runtime.spawn("test-agent", model="claude")
        
        assert agent.id.startswith("agent-")
        assert agent.config.name == "test-agent"
        assert agent.config.model == "claude"
        assert agent.status == AgentStatus.PENDING
        
        await runtime.stop()

    def test_get_agent(self, runtime: AgentRuntime) -> None:
        agent = runtime.spawn("test", model="claude")
        fetched = runtime.get_agent(agent.id)
        assert fetched is agent

    def test_get_missing_agent(self, runtime: AgentRuntime) -> None:
        fetched = runtime.get_agent("nonexistent")
        assert fetched is None

    def test_list_agents(self, runtime: AgentRuntime) -> None:
        agent1 = runtime.spawn("agent-1", model="claude")
        agent2 = runtime.spawn("agent-2", model="copilot")
        
        agents = runtime.list_agents()
        assert len(agents) == 2
        assert agent1 in agents
        assert agent2 in agents

    def test_list_agents_filtered_by_name(self, runtime: AgentRuntime) -> None:
        runtime.spawn("search-agent", model="claude")
        runtime.spawn("other-agent", model="claude")
        
        agents = runtime.list_agents(name="search-agent")
        assert len(agents) == 1
        assert agents[0].config.name == "search-agent"

    @pytest.mark.asyncio
    async def test_agent_lifecycle(self, runtime: AgentRuntime) -> None:
        await runtime.start()
        agent = runtime.spawn("lifecycle-test", model="claude")
        
        # Initial state
        assert agent.status == AgentStatus.PENDING
        
        # Mock the client
        with patch.object(agent, '_client') as mock_client:
            mock_client.send = AsyncMock(return_value=AsyncMock(text="result"))
            
            await agent.start()
            assert agent.status == AgentStatus.WAITING
            
            # Run would change status but we're mocking
            # Just verify state tracking works
            agent._update_state()
            state = agent.get_state()
            assert state.name == "lifecycle-test"
        
        await runtime.stop()

    @pytest.mark.asyncio
    async def test_message_routing(self, runtime: AgentRuntime) -> None:
        await runtime.start()
        
        agent1 = runtime.spawn("sender", model="claude")
        agent2 = runtime.spawn("receiver", model="claude")
        
        # Send message from agent1 to agent2
        await agent1.send_message(agent2.id, "hello")
        
        # Give message bus time to process
        await asyncio.sleep(0.1)
        
        # Check agent2 received it
        assert not agent2._message_queue.empty()
        
        await runtime.stop()

    @pytest.mark.asyncio
    async def test_broadcast_message(self, runtime: AgentRuntime) -> None:
        await runtime.start()
        
        agent1 = runtime.spawn("broadcaster", model="claude")
        agent2 = runtime.spawn("listener1", model="claude")
        agent3 = runtime.spawn("listener2", model="claude")
        
        await agent1.send_message("broadcast", "hello all")
        await asyncio.sleep(0.1)
        
        # Both listeners should have message, not broadcaster
        assert not agent2._message_queue.empty()
        assert not agent3._message_queue.empty()
        # Sender shouldn't get their own broadcast
        assert agent1._message_queue.empty()
        
        await runtime.stop()


class TestAgentStatePersistence:
    @pytest.mark.asyncio
    async def test_state_saved_to_memory(self, runtime: AgentRuntime) -> None:
        await runtime.start()
        agent = runtime.spawn("state-test", model="claude")
        
        agent._update_state()
        
        # Check memory has the state
        state_data = agent.memory.get(f"agent_state_{agent.id}", namespace="agent:runtime")
        assert state_data is not None
        assert state_data["name"] == "state-test"
        assert state_data["status"] == "PENDING"
        
        await runtime.stop()

    def test_get_agent_status_from_memory(self, runtime: AgentRuntime) -> None:
        # Create agent, it saves state
        agent = runtime.spawn("memory-test", model="claude")
        agent._update_state()
        
        # Create new runtime instance (simulating restart)
        new_runtime = AgentRuntime(user=runtime.user)
        state = new_runtime.get_agent_status(agent.id)
        
        assert state is not None
        assert state.name == "memory-test"


class TestAgentPromptBuilding:
    def test_build_prompt_with_memory(self, runtime: AgentRuntime) -> None:
        agent = runtime.spawn("prompt-test", model="claude")
        
        # Add some memory
        agent.memory.set("context", {"repo": "test"}, namespace="default:tasks")
        
        prompt = agent._build_prompt(
            "do something",
            {"memory:1": {"data": "value"}},
            {"extra": "context"}
        )
        
        assert "do something" in prompt
        assert "Relevant Context" in prompt
        assert "Task Context" in prompt


class TestAgentMemoryIntegration:
    def test_agent_stores_task_in_memory(self, runtime: AgentRuntime) -> None:
        agent = runtime.spawn("task-test", model="claude")
        
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
        agent = runtime.spawn("load-test", model="claude")
        
        # Store some tasks
        for i in range(3):
            agent.memory.set(
                f"task_{i}",
                {"prompt": f"task {i}"},
                namespace="default:tasks"
            )
        
        memory = agent._load_relevant_memory("test prompt")
        
        # Should have loaded tasks (up to 5)
        assert len(memory) > 0


class TestAgentErrorHandling:
    @pytest.mark.asyncio
    async def test_agent_handles_run_error(self, runtime: AgentRuntime) -> None:
        await runtime.start()
        agent = runtime.spawn("error-test", model="claude")
        
        with patch.object(agent, '_client') as mock_client:
            mock_client.send = AsyncMock(side_effect=Exception("API error"))
            
            await agent.start()
            
            with pytest.raises(Exception, match="API error"):
                await agent.run("test prompt")
            
            assert agent.status == AgentStatus.FAILED
            assert agent._error is not None
        
        await runtime.stop()


class TestAgentStreaming:
    @pytest.mark.asyncio
    async def test_stream_chunks(self, runtime: AgentRuntime) -> None:
        await runtime.start()
        agent = runtime.spawn("stream-test", model="claude")
        
        async def mock_stream(*args, **kwargs):
            yield AsyncMock(text="Hello ")
            yield AsyncMock(text="world!")
        
        with patch.object(agent, '_client') as mock_client:
            mock_client.stream = mock_stream
            
            await agent.start()
            
            chunks = []
            async for chunk in agent.stream("say hello"):
                chunks.append(chunk)
            
            assert len(chunks) == 2
            assert chunks[0] == "Hello "
            assert chunks[1] == "world!"
        
        await runtime.stop()
