"""
sdk/agents — Agent runtime and lifecycle management for Obscura.

Spawn agents, manage their state, coordinate via shared memory.
Think of it as a "process manager for AI agents."

Usage::

    from obscura.agent.agents import AgentRuntime, Agent

    runtime = AgentRuntime()

    # Spawn an agent
    agent = runtime.spawn(
        name="code-reviewer",
        model="claude",
        system_prompt="You are a code reviewer...",
        memory_namespace="project:obscura"
    )

    # Run the agent
    result = await agent.run("Review this PR: ...")

    # Check what other agents are doing
    status = runtime.get_agent_status(agent.id)
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import UTC, datetime
from enum import Enum, auto
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, cast

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from obscura.core.types import AgentEvent, AgentEventKind
from obscura.core.types import ToolSpec
from obscura.core.paths import resolve_obscura_mcp_dir
from obscura.auth.models import AuthenticatedUser
from obscura.core.client import ObscuraClient
from obscura.memory import MemoryStore

if TYPE_CHECKING:
    from obscura.providers.mcp_backend import MCPBackend
    from obscura.heartbeat.client import AgentHeartbeatClient
    from obscura.tools.providers import ToolProviderRegistry


def _default_mcp_servers() -> list[dict[str, Any]]:
    return []


def _default_server_names() -> list[str]:
    return []


def _default_mcp_config_path() -> str:
    return str(resolve_obscura_mcp_dir())


class AgentStatus(Enum):
    """Agent lifecycle states."""

    PENDING = auto()  # Created but not started
    RUNNING = auto()  # Currently executing
    WAITING = auto()  # Blocked on I/O or memory
    COMPLETED = auto()  # Finished successfully
    FAILED = auto()  # Error occurred
    STOPPED = auto()  # Manually stopped


class MCPConfig(BaseModel):
    """Configuration for MCP (Model Context Protocol) integration."""

    enabled: bool = False
    servers: list[dict[str, Any]] = Field(default_factory=_default_mcp_servers)
    config_path: str = Field(default_factory=_default_mcp_config_path)
    server_names: list[str] = Field(default_factory=_default_server_names)
    primary_server_name: str = "github"
    auto_discover: bool = True
    resolve_env: bool = True
    """List of MCP server configurations. Each server config should have:
    - transport: "stdio" or "sse"
    - command: str (for stdio)
    - args: list[str] (for stdio)
    - url: str (for sse)
    - env: dict[str, str] (optional)
    """


class AgentConfig(BaseModel):
    """Configuration for an agent instance."""

    name: str
    model: str  # "copilot", "claude", "localllm", "openai", or "moonshot"
    system_prompt: str = ""
    memory_namespace: str = "default"
    max_iterations: int = 10
    timeout_seconds: float = 300.0
    tools: list[str] = []
    parent_agent_id: str | None = None
    tags: list[str] = []
    mcp: MCPConfig = MCPConfig()
    enable_system_tools: bool = True
    a2a_remote_tools: dict[str, Any] = Field(default_factory=dict)


class AgentState(BaseModel):
    """Serializable state of an agent."""

    agent_id: str
    name: str
    status: AgentStatus
    created_at: datetime
    updated_at: datetime
    iteration_count: int = 0
    memory_snapshot: dict[str, Any] = {}
    error_message: str | None = None


class AgentMessage(BaseModel):
    """Message passed between agents or from user to agent."""

    source: str  # agent_id or "user" or "system"
    target: str  # agent_id or "broadcast"
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    message_type: str = "text"  # "text", "command", "result", "error"


class Agent:
    """
    A single agent instance with its own memory and lifecycle.

    Agents can:
    - Run tasks and maintain conversation state
    - Read/write to shared memory (scoped by user)
    - Spawn child agents
    - Communicate with other agents via message bus
    """

    def __init__(
        self,
        agent_id: str,
        config: AgentConfig,
        user: AuthenticatedUser,
        runtime: AgentRuntime,
    ):
        self.id = agent_id
        self.config = config
        self.user = user
        self.runtime = runtime
        self.status = AgentStatus.PENDING
        self.created_at = datetime.now(UTC)
        self.updated_at = self.created_at
        self.iteration_count = 0
        self._client: ObscuraClient | None = None
        self._mcp_backend: MCPBackend | None = None
        self._tool_provider_registry: ToolProviderRegistry | None = None
        self._message_queue: asyncio.Queue[AgentMessage] = asyncio.Queue()
        self._task: asyncio.Task[Any] | None = None
        self._heartbeat_client: AgentHeartbeatClient | None = None
        self._heartbeat_enabled: bool = True
        self._current_prompt: str = ""
        self._result: Any = None
        self._error: Exception | None = None

    # -- Observability/test accessors -----------------------------------
    @property
    def client(self) -> ObscuraClient | None:
        """Testing/observability: injected client (read/write)."""
        return self._client

    @client.setter
    def client(self, value: ObscuraClient | None) -> None:
        self._client = value

    @property
    def mcp_backend(self) -> MCPBackend | None:
        """Read-only MCP backend reference for tests."""
        return self._mcp_backend

    @mcp_backend.setter
    def mcp_backend(self, backend: MCPBackend | None) -> None:
        self._mcp_backend = backend

    @property
    def heartbeat_client(self) -> AgentHeartbeatClient | None:
        """Read-only heartbeat client."""
        return self._heartbeat_client

    @heartbeat_client.setter
    def heartbeat_client(self, client: AgentHeartbeatClient | None) -> None:
        self._heartbeat_client = client

    @property
    def heartbeat_enabled(self) -> bool:
        """Flag controlling heartbeat startup (settable for tests)."""
        return self._heartbeat_enabled

    @heartbeat_enabled.setter
    def heartbeat_enabled(self, enabled: bool) -> None:
        self._heartbeat_enabled = enabled

    @property
    def message_queue(self) -> asyncio.Queue[AgentMessage]:
        """Access to the inbound message queue for test inspection."""
        return self._message_queue

    @message_queue.setter
    def message_queue(self, queue: asyncio.Queue[AgentMessage]) -> None:
        self._message_queue = queue

    @property
    def task(self) -> asyncio.Task[Any] | None:
        """Access to the in-flight asyncio task (if any)."""
        return self._task

    @task.setter
    def task(self, value: asyncio.Task[Any] | None) -> None:
        self._task = value

    @property
    def result(self) -> Any:
        """Latest result produced by the agent."""
        return self._result

    @property
    def error(self) -> Exception | None:
        """Latest error captured by the agent."""
        return self._error

    @error.setter
    def error(self, err: Exception | None) -> None:
        self._error = err

    @property
    def memory(self) -> MemoryStore:
        """Get the agent's memory store."""
        return MemoryStore.for_user(self.user)

    def list_registered_tools(self) -> list[ToolSpec]:
        """Return all tools currently registered on the underlying client."""
        if self._client is None:
            return []
        return self._client.list_tools()

    async def start(self) -> None:
        """Initialize the agent and connect to backend."""
        from obscura.tools.providers import (
            A2ARemoteToolProvider,
            MCPToolProvider,
            SystemToolProvider,
            ToolProviderContext,
            ToolProviderRegistry,
        )

        self._client = ObscuraClient(
            self.config.model,
            system_prompt=self.config.system_prompt,
            user=self.user,
        )
        await self._client.start()

        provider_registry = ToolProviderRegistry()

        server_configs: list[dict[str, Any]] = list(self.config.mcp.servers)
        if (
            self.config.mcp.enabled
            and not server_configs
            and self.config.mcp.auto_discover
        ):
            from obscura.integrations.mcp.config_loader import (
                build_runtime_server_configs,
                discover_mcp_servers,
            )

            discovered = discover_mcp_servers(
                self.config.mcp.config_path,
                resolve_env=self.config.mcp.resolve_env,
            )
            selected_names = (
                self.config.mcp.server_names
                if self.config.mcp.server_names
                else None
            )
            server_configs = build_runtime_server_configs(
                discovered,
                selected_names=selected_names,
                primary_server_name=self.config.mcp.primary_server_name,
            )

        if self.config.mcp.enabled and server_configs:
            from obscura.integrations.mcp.types import MCPConnectionConfig, MCPTransportType

            mcp_configs: list[MCPConnectionConfig] = []
            for server_config in server_configs:
                transport = MCPTransportType(server_config.get("transport", "stdio"))
                config = MCPConnectionConfig(
                    transport=transport,
                    command=server_config.get("command"),
                    args=server_config.get("args", []),
                    url=server_config.get("url"),
                    env=server_config.get("env", {}),
                )
                mcp_configs.append(config)

            provider_registry.add(MCPToolProvider(mcp_configs))

        if self.config.enable_system_tools:
            provider_registry.add(SystemToolProvider())

        a2a_remote_config = self.config.a2a_remote_tools
        if bool(a2a_remote_config.get("enabled", False)):
            raw_urls = a2a_remote_config.get("urls", [])
            urls = (
                [str(url) for url in cast(list[Any], raw_urls)]
                if isinstance(raw_urls, list)
                else []
            )
            if urls:
                provider_registry.add(
                    A2ARemoteToolProvider(
                        urls=urls,
                        auth_token=(
                            str(a2a_remote_config["auth_token"])
                            if "auth_token" in a2a_remote_config
                            and a2a_remote_config["auth_token"] is not None
                            else None
                        ),
                    )
                )

        await provider_registry.install_all(ToolProviderContext(agent=self))
        self._tool_provider_registry = provider_registry

        # Initialize heartbeat client if enabled
        if self._heartbeat_enabled:
            await self._start_heartbeat()

        self.status = AgentStatus.WAITING
        self._update_state()

    async def run(self, prompt: str, **context: Any) -> Any:
        """
        Execute the agent on a task.

        Stores context in memory, runs the agent, captures result.
        """
        assert self._client is not None, "Agent.start() must be called before run()"
        self._current_prompt = prompt
        self.status = AgentStatus.RUNNING
        self._update_state()

        # Store task context in memory
        self.memory.set(
            f"task_{self.iteration_count}",
            {
                "prompt": prompt,
                "context": context,
                "started_at": datetime.now(UTC).isoformat(),
            },
            namespace=f"{self.config.memory_namespace}:tasks",
        )

        try:
            # Load relevant memory into context
            relevant_memory = self._load_relevant_memory(prompt)

            # Build the full prompt with memory context
            full_prompt = self._build_prompt(prompt, relevant_memory, context)

            # Execute with timeout enforcement
            message = await asyncio.wait_for(
                self._client.send(full_prompt),
                timeout=self.config.timeout_seconds,
            )
            self._result = message.text

            # Store result
            self.memory.set(
                f"result_{self.iteration_count}",
                {
                    "result": self._result,
                    "completed_at": datetime.now(UTC).isoformat(),
                },
                namespace=f"{self.config.memory_namespace}:tasks",
            )

            self.status = AgentStatus.COMPLETED
            self.iteration_count += 1

            return self._result

        except asyncio.TimeoutError:
            self._error = TimeoutError(
                f"Agent '{self.config.name}' timed out after {self.config.timeout_seconds}s"
            )
            self.status = AgentStatus.FAILED
            raise self._error
        except Exception as e:
            self._error = e
            self.status = AgentStatus.FAILED
            raise
        finally:
            self._update_state()

    async def stream(self, prompt: str, **context: Any) -> AsyncIterator[str]:
        """Stream the agent's response."""
        assert self._client is not None, "Agent.start() must be called before stream()"
        self._current_prompt = prompt
        self.status = AgentStatus.RUNNING
        self._update_state()

        # Store task context in memory
        self.memory.set(
            f"task_{self.iteration_count}",
            {
                "prompt": prompt,
                "context": context,
                "started_at": datetime.now(UTC).isoformat(),
                "mode": "stream",
            },
            namespace=f"{self.config.memory_namespace}:tasks",
        )

        try:
            relevant_memory = self._load_relevant_memory(prompt)
            full_prompt = self._build_prompt(prompt, relevant_memory, context)

            async for chunk in self._client.stream(full_prompt):
                yield chunk.text if hasattr(chunk, "text") else str(chunk)

            self.status = AgentStatus.COMPLETED
            self.iteration_count += 1
        except Exception as e:
            self._error = e
            self.status = AgentStatus.FAILED
            raise
        finally:
            self._update_state()

    async def run_loop(
        self,
        prompt: str,
        *,
        max_turns: int | None = None,
        on_confirm: Callable[..., Any] | None = None,
        **context: Any,
    ) -> str:
        """Run the agent in an iterative loop with automatic tool execution.

        Unlike :meth:`run` (single-shot send/receive), this method drives
        the model across multiple turns. When the model calls a tool, the
        loop executes the handler, feeds the result back, and lets the model
        continue.

        Returns the concatenated text output from all turns.
        """
        assert self._client is not None, (
            "Agent.start() must be called before run_loop()"
        )
        self._current_prompt = prompt
        self.status = AgentStatus.RUNNING
        self._update_state()

        if max_turns is None:
            max_turns = self.config.max_iterations

        self.memory.set(
            f"task_{self.iteration_count}",
            {
                "prompt": prompt,
                "context": context,
                "started_at": datetime.now(UTC).isoformat(),
                "mode": "agent_loop",
            },
            namespace=f"{self.config.memory_namespace}:tasks",
        )

        try:
            relevant_memory = self._load_relevant_memory(prompt)
            full_prompt = self._build_prompt(prompt, relevant_memory, context)

            result = await asyncio.wait_for(
                self._client.run_loop_to_completion(
                    full_prompt,
                    max_turns=max_turns,
                    on_confirm=on_confirm,
                ),
                timeout=self.config.timeout_seconds,
            )

            self._result = result
            self.memory.set(
                f"result_{self.iteration_count}",
                {
                    "result": self._result,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "mode": "agent_loop",
                },
                namespace=f"{self.config.memory_namespace}:tasks",
            )

            self.status = AgentStatus.COMPLETED
            self.iteration_count += 1
            return self._result

        except asyncio.TimeoutError:
            self._error = TimeoutError(
                f"Agent '{self.config.name}' timed out after {self.config.timeout_seconds}s"
            )
            self.status = AgentStatus.FAILED
            raise self._error
        except Exception as e:
            self._error = e
            self.status = AgentStatus.FAILED
            raise
        finally:
            self._update_state()

    async def stream_loop(
        self,
        prompt: str,
        *,
        max_turns: int | None = None,
        on_confirm: Callable[..., Any] | None = None,
        **context: Any,
    ) -> AsyncIterator[AgentEvent]:
        """Stream agent loop events including tool calls and results.

        Yields :class:`AgentEvent` objects for every interesting thing
        that happens: text deltas, tool calls, tool results, turn
        boundaries, and final completion.
        """
        assert self._client is not None, (
            "Agent.start() must be called before stream_loop()"
        )
        self._current_prompt = prompt
        self.status = AgentStatus.RUNNING
        self._update_state()

        if max_turns is None:
            max_turns = self.config.max_iterations

        self.memory.set(
            f"task_{self.iteration_count}",
            {
                "prompt": prompt,
                "context": context,
                "started_at": datetime.now(UTC).isoformat(),
                "mode": "stream_loop",
            },
            namespace=f"{self.config.memory_namespace}:tasks",
        )

        try:
            relevant_memory = self._load_relevant_memory(prompt)
            full_prompt = self._build_prompt(prompt, relevant_memory, context)

            text_parts: list[str] = []
            async for event in self._client.run_loop(
                full_prompt,
                max_turns=max_turns,
                on_confirm=on_confirm,
            ):
                if event.kind == AgentEventKind.TEXT_DELTA:
                    text_parts.append(event.text)
                yield event

            self._result = "".join(text_parts)
            self.memory.set(
                f"result_{self.iteration_count}",
                {
                    "result": self._result,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "mode": "stream_loop",
                },
                namespace=f"{self.config.memory_namespace}:tasks",
            )

            self.status = AgentStatus.COMPLETED
            self.iteration_count += 1

        except Exception as e:
            self._error = e
            self.status = AgentStatus.FAILED
            raise
        finally:
            self._update_state()

    def _load_relevant_memory(self, prompt: str) -> dict[str, Any]:
        """Load memory relevant to the current task."""
        relevant: dict[str, Any] = {}

        # Use vector search with reranking if available
        if hasattr(self, "vector_memory"):
            try:
                from obscura.vector_memory import VectorMemoryEntry

                recall_fn: Callable[..., list[VectorMemoryEntry]] = getattr(
                    self, "recall"
                )
                memories = recall_fn(
                    prompt,
                    top_k=5,
                    use_reranking=True,
                    recency_weight=0.2,
                )
                for mem in memories:
                    relevant[f"semantic:{mem.key.key}"] = {
                        "text": mem.text,
                        "score": mem.final_score,
                        "type": mem.memory_type,
                    }
            except Exception:
                pass  # Fall through to KV search

        # Also pull recent tasks from KV store
        tasks_ns = f"{self.config.memory_namespace}:tasks"
        keys = self.memory.list_keys(namespace=tasks_ns)

        for key in sorted(keys, key=lambda k: k.key, reverse=True)[:3]:
            value = self.memory.get(key.key, namespace=key.namespace)
            if value:
                relevant[f"task:{str(key)}"] = value

        # Fallback text search if no vector results
        if not any(k.startswith("semantic:") for k in relevant):
            search_results = self.memory.search(prompt[:50])
            for key, value in search_results[:3]:
                relevant[str(key)] = value

        return relevant

    def _build_prompt(
        self, prompt: str, memory: dict[str, Any], context: dict[str, Any]
    ) -> str:
        """Build the full prompt with memory and context."""
        parts: list[str] = []

        # Add memory context
        if memory:
            parts.append("## Relevant Context from Memory:")
            for key, value in memory.items():
                parts.append(f"- {key}: {value}")
            parts.append("")

        # Add explicit context
        if context:
            parts.append("## Task Context:")
            for key, value in context.items():
                parts.append(f"- {key}: {value}")
            parts.append("")

        # Add the actual prompt
        parts.append(f"## Task:\n{prompt}")

        return "\n".join(parts)

    async def send_message(self, target: str, content: str) -> None:
        """Send a message to another agent or broadcast."""
        message = AgentMessage(
            source=self.id, target=target, content=content, message_type="text"
        )
        await self.runtime.route_message(message)

    async def receive_messages(self) -> AsyncIterator[AgentMessage]:
        """Receive messages sent to this agent."""
        while True:
            try:
                message = await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
                yield message
            except asyncio.TimeoutError:
                if self.status in (
                    AgentStatus.COMPLETED,
                    AgentStatus.FAILED,
                    AgentStatus.STOPPED,
                ):
                    break

    def enqueue_message(self, message: AgentMessage) -> None:
        """Add message to queue."""
        try:
            self._message_queue.put_nowait(message)
        except asyncio.QueueFull:
            logger.warning(
                "Message queue full for agent %s, dropping message from %s",
                self.id,
                message.source,
            )

    def _update_state(self) -> None:
        """Persist agent state to memory."""
        self.updated_at = datetime.now(UTC)
        state = AgentState(
            agent_id=self.id,
            name=self.config.name,
            status=self.status,
            created_at=self.created_at,
            updated_at=self.updated_at,
            iteration_count=self.iteration_count,
            memory_snapshot={},  # Could snapshot key memory here
            error_message=str(self._error) if self._error else None,
        )
        self.memory.set(
            f"agent_state_{self.id}",
            {
                "agent_id": state.agent_id,
                "name": state.name,
                "status": state.status.name,
                "created_at": state.created_at.isoformat(),
                "updated_at": state.updated_at.isoformat(),
                "iteration_count": state.iteration_count,
                "error_message": state.error_message,
            },
            namespace="agent:runtime",
        )

    async def _start_heartbeat(self) -> None:
        """Initialize and start the heartbeat client."""
        # Get monitor URL from environment or use default
        monitor_url = os.environ.get("OBSCURA_HEARTBEAT_URL", "http://localhost:8080")
        interval = int(os.environ.get("OBSCURA_HEARTBEAT_INTERVAL", "30"))

        try:
            from obscura.heartbeat import AgentHeartbeatClient

            self._heartbeat_client = AgentHeartbeatClient(
                agent_id=self.id,
                monitor_url=monitor_url,
                interval=interval,
                tags=self.config.tags,
            )
            await self._heartbeat_client.start()
            logger.debug(f"Started heartbeat client for agent {self.id}")
        except Exception as e:
            logger.warning(f"Failed to start heartbeat client for agent {self.id}: {e}")
            self._heartbeat_client = None

    async def stop(self) -> None:
        """Stop the agent and cleanup."""
        self.status = AgentStatus.STOPPED
        if self._client:
            try:
                await self._client.stop()
            except RuntimeError as e:
                # Ignore cancel scope errors from underlying SDK
                if "cancel scope" not in str(e):
                    raise
        if self._tool_provider_registry:
            from obscura.tools.providers import ToolProviderContext

            await self._tool_provider_registry.uninstall_all(
                ToolProviderContext(agent=self)
            )
            self._tool_provider_registry = None
        elif self._mcp_backend:
            await self._mcp_backend.stop()
            self._mcp_backend = None
        if self._heartbeat_client:
            await self._heartbeat_client.stop()
            self._heartbeat_client = None
        if self._task and not self._task.done():
            self._task.cancel()
        self._update_state()

    async def stop_graceful(self, timeout: float = 5.0) -> None:
        """Stop the agent gracefully with a timeout."""
        try:
            await asyncio.wait_for(self.stop(), timeout=timeout)
        except asyncio.TimeoutError:
            # Force stop
            if self._task and not self._task.done():
                self._task.cancel()
            self.status = AgentStatus.STOPPED
            self._update_state()

    def get_state(self) -> AgentState:
        """Get current agent state."""
        return AgentState(
            agent_id=self.id,
            name=self.config.name,
            status=self.status,
            created_at=self.created_at,
            updated_at=self.updated_at,
            iteration_count=self.iteration_count,
            error_message=str(self._error) if self._error else None,
        )

    def refresh_state(self) -> AgentState:
        """Recalculate and return the current state (testing/observability)."""
        self._update_state()
        return self.get_state()

    # Public wrappers for internal helpers (used in tests/observability)
    def build_prompt(
        self, prompt: str, relevant_memory: dict[str, Any], context: dict[str, Any]
    ) -> str:
        """Public wrapper around _build_prompt."""
        return self._build_prompt(prompt, relevant_memory, context)

    def load_relevant_memory(self, prompt: str) -> dict[str, Any]:
        """Public wrapper around _load_relevant_memory."""
        return self._load_relevant_memory(prompt)


class AgentRuntime:
    """
    Runtime environment for managing multiple agents.

    Think of this as a "process manager" for AI agents:
    - Spawn new agents
    - Track running agents
    - Route messages between agents
    - Cleanup stopped agents
    """

    def __init__(self, user: AuthenticatedUser | None = None):
        self.user = user
        self._agents: dict[str, Agent] = {}
        self._lock = asyncio.Lock()
        self._message_bus: asyncio.Queue[AgentMessage] = asyncio.Queue()
        self._bus_task: asyncio.Task[None] | None = None

    # Public observability helpers
    @property
    def agents(self) -> dict[str, Agent]:
        """Read-only view of managed agents."""
        return self._agents

    @property
    def bus_task(self) -> asyncio.Task[None] | None:
        """Access the running message-bus task."""
        return self._bus_task

    @property
    def message_bus(self) -> asyncio.Queue[AgentMessage]:
        """Access the message bus queue for testing."""
        return self._message_bus

    async def start(self) -> None:
        """Start the message bus."""
        self._bus_task = asyncio.create_task(self._message_bus_loop())

    async def stop(self) -> None:
        """Stop all agents and cleanup."""
        if self._bus_task:
            self._bus_task.cancel()
            try:
                await self._bus_task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            for agent in list(self._agents.values()):
                await agent.stop()
            self._agents.clear()

    def spawn(
        self,
        name: str,
        model: str = "copilot",
        system_prompt: str = "",
        memory_namespace: str = "default",
        parent_agent_id: str | None = None,
        **config_kwargs: Any,
    ) -> Agent:
        """
        Spawn a new agent with the given configuration.

        Returns the agent instance immediately (not started yet).
        Call agent.start() then agent.run() to execute.
        """
        agent_id = f"agent-{uuid.uuid4().hex[:8]}"

        config = AgentConfig(
            name=name,
            model=model,
            system_prompt=system_prompt,
            memory_namespace=memory_namespace,
            parent_agent_id=parent_agent_id,
            **config_kwargs,
        )

        if self.user is None:
            raise RuntimeError("AgentRuntime requires a user to spawn agents")
        agent = Agent(agent_id, config, self.user, self)

        # Store reference
        self._agents[agent_id] = agent

        # Register with heartbeat monitor if enabled
        heartbeat_enabled = (
            os.environ.get("OBSCURA_HEARTBEAT_ENABLED", "true").lower() == "true"
        )
        if heartbeat_enabled:
            try:
                from obscura.heartbeat import get_default_monitor

                monitor = get_default_monitor()
                # Schedule registration - can't be async in sync method
                asyncio.create_task(monitor.register_agent(agent_id))
                logger.debug(f"Registered agent {agent_id} with heartbeat monitor")
            except Exception as e:
                logger.warning(
                    f"Failed to register agent {agent_id} with heartbeat monitor: {e}"
                )

        return agent

    async def spawn_and_run(
        self,
        name: str,
        prompt: str,
        model: str = "copilot",
        system_prompt: str = "",
        **kwargs: Any,
    ) -> tuple[Agent, Any]:
        """Convenience: spawn, start, run, and return result."""
        agent = self.spawn(name, model, system_prompt, **kwargs)
        await agent.start()
        result = await agent.run(prompt)
        return agent, result

    def get_agent(self, agent_id: str) -> Agent | None:
        """Get an agent by ID."""
        return self._agents.get(agent_id)

    def list_agents(
        self, status: AgentStatus | None = None, name: str | None = None
    ) -> list[Agent]:
        """List all agents, optionally filtered."""
        agents = list(self._agents.values())

        if status:
            agents = [a for a in agents if a.status == status]

        if name:
            agents = [a for a in agents if a.config.name == name]

        return agents

    def get_agent_status(self, agent_id: str) -> AgentState | None:
        """Get the current state of an agent."""
        agent = self._agents.get(agent_id)
        if agent:
            return agent.get_state()

        # Try to load from memory (agent may have crashed/restarted)
        if self.user:
            memory = MemoryStore.for_user(self.user)
            state_data = memory.get(
                f"agent_state_{agent_id}", namespace="agent:runtime"
            )
            if state_data:
                return AgentState(
                    agent_id=state_data["agent_id"],
                    name=state_data["name"],
                    status=AgentStatus[state_data["status"]],
                    created_at=datetime.fromisoformat(state_data["created_at"]),
                    updated_at=datetime.fromisoformat(state_data["updated_at"]),
                    iteration_count=state_data.get("iteration_count", 0),
                    error_message=state_data.get("error_message"),
                )

        return None

    async def route_message(self, message: AgentMessage) -> None:
        """Route a message to its target agent(s)."""
        await self._message_bus.put(message)

    async def _message_bus_loop(self) -> None:
        """Background task to route messages."""
        while True:
            try:
                message = await self._message_bus.get()

                if message.target == "broadcast":
                    # Send to all agents except sender
                    for agent_id, agent in self._agents.items():
                        if agent_id != message.source:
                            agent.enqueue_message(message)
                else:
                    # Send to specific agent
                    target_agent = self._agents.get(message.target)
                    if target_agent:
                        target_agent.enqueue_message(message)
                    else:
                        logger.warning(
                            "Message target agent %s not found (from %s)",
                            message.target,
                            message.source,
                        )

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in message bus loop")

    async def wait_for_agents(
        self, agent_ids: list[str], timeout: float | None = None
    ) -> list[AgentState]:
        """Wait for multiple agents to complete."""

        async def wait_one(agent_id: str) -> AgentState:
            while True:
                state = self.get_agent_status(agent_id)
                if state and state.status in (
                    AgentStatus.COMPLETED,
                    AgentStatus.FAILED,
                    AgentStatus.STOPPED,
                ):
                    return state
                await asyncio.sleep(0.1)

        tasks = [asyncio.create_task(wait_one(aid)) for aid in agent_ids]

        if timeout:
            done, pending = await asyncio.wait(
                tasks, timeout=timeout, return_when=asyncio.ALL_COMPLETED
            )
            for task in pending:
                task.cancel()
            return [
                task.result() for task in done if task.done() and not task.cancelled()
            ]
        else:
            results = await asyncio.gather(*tasks)
            return list(results)
