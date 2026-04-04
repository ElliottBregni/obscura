"""obscura.tools.system.delegation — Grounded agent delegation tool.

Provides ``build_delegate_tool_spec()`` which dynamically creates a
``ToolSpec`` with an enum-constrained ``agent`` parameter populated from
live peer discovery.  The LLM can ONLY pick agents that actually exist,
preventing hallucinated agent names.

Also provides ``build_agent_cards_section()`` which generates a text block
suitable for injection into the prompt assembler's ``context_instructions``
section so the LLM knows *when* to delegate.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

from obscura.agent.peers import (
    AgentRef,
    RemoteAgentRef,
    UnixSocketAgentRef,
)
from obscura.core.types import ToolSpec

if TYPE_CHECKING:
    from obscura.agent.agents import AgentRuntime
    from obscura.agent.peers import PeerRegistry

logger = logging.getLogger(__name__)

# Type alias for the union of all peer ref kinds.
AnyAgentRef = AgentRef | RemoteAgentRef | UnixSocketAgentRef


def build_delegate_tool_spec(
    runtime: AgentRuntime,
    peer_registry: PeerRegistry,
    remote_refs: list[RemoteAgentRef] | None = None,
    unix_socket_refs: list[UnixSocketAgentRef] | None = None,
) -> ToolSpec:
    """Build a ``delegate_to_agent`` ToolSpec with a dynamic agent enum.

    The ``agent`` parameter's enum is populated from:
    - Local peers discovered via ``peer_registry.discover()``
    - Configured remote A2A peers (``remote_refs``)
    - Configured Unix socket peers (``unix_socket_refs``)

    The handler routes calls to the correct transport based on the
    peer ref's ``kind`` field.
    """
    # Discover all available agents and build the name → ref map.
    agent_map: dict[str, AnyAgentRef] = {}

    for ref in peer_registry.discover():
        agent_map[ref.name] = ref

    for ref in remote_refs or []:
        name = ref.name or ref.url
        agent_map[name] = ref

    for ref in unix_socket_refs or []:
        name = ref.name or ref.socket_path
        if ref.status == "available":
            agent_map[name] = ref

    agent_names = sorted(agent_map.keys())

    # Build constrained JSON Schema.
    agent_property: dict[str, Any] = {
        "type": "string",
        "description": (
            "Name of the agent to delegate to. MUST be one of the listed values."
        ),
    }
    if agent_names:
        agent_property["enum"] = agent_names

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "agent": agent_property,
            "prompt": {
                "type": "string",
                "description": "The task or question to send to the target agent.",
            },
            "mode": {
                "type": "string",
                "enum": ["blocking", "streaming"],
                "default": "blocking",
                "description": "Invocation mode.",
            },
        },
        "required": ["agent", "prompt"],
    }

    # Capture runtime and map in the handler closure.
    _runtime = runtime
    _agent_map = agent_map

    async def _handle_delegate(
        agent: str,
        prompt: str,
        mode: str = "blocking",
        **_kwargs: Any,
    ) -> str:
        return await _execute_delegation(
            _runtime,
            _agent_map,
            agent,
            prompt,
            mode,
        )

    return ToolSpec(
        name="delegate_to_agent",
        description=(
            "Delegate a task to another agent in the system. "
            "More efficient than spawning a subprocess — uses in-process "
            "invocation for local agents or direct transport for remote ones."
        ),
        parameters=schema,
        handler=_handle_delegate,
        timeout_seconds=300.0,
    )


async def _execute_delegation(
    runtime: AgentRuntime,
    agent_map: dict[str, AnyAgentRef],
    agent: str,
    prompt: str,
    mode: str,
) -> str:
    """Route a delegation call to the correct transport."""
    ref = agent_map.get(agent)
    if ref is None:
        return json.dumps(
            {
                "ok": False,
                "error": "agent_not_found",
                "message": f"Agent '{agent}' is not available. "
                f"Available agents: {sorted(agent_map.keys())}",
                "agent": agent,
                "prompt": prompt,
            },
        )

    try:
        if isinstance(ref, AgentRef):
            result = await _invoke_local(runtime, ref, prompt, mode)
        elif isinstance(ref, RemoteAgentRef):
            result = await _invoke_remote(ref, prompt)
        elif isinstance(ref, UnixSocketAgentRef):
            result = await _invoke_unix_socket(ref, prompt)
        else:
            return json.dumps(
                {
                    "ok": False,
                    "error": "unsupported_transport",
                    "message": f"Unknown agent ref kind: {type(ref).__name__}",
                    "agent": agent,
                    "prompt": prompt,
                },
            )

        return json.dumps(
            {
                "ok": True,
                "agent": agent,
                "transport": ref.kind,
                "result": result,
                "prompt": prompt,
            },
        )
    except Exception as exc:
        return json.dumps(
            {
                "ok": False,
                "error": type(exc).__name__,
                "message": str(exc),
                "agent": agent,
                "prompt": prompt,
            },
        )


async def _invoke_local(
    runtime: AgentRuntime,
    ref: AgentRef,
    prompt: str,
    mode: str,
) -> str:
    """Invoke a local peer agent in-process."""
    use_loop = mode != "streaming"
    return await runtime.invoke_peer(
        ref,
        prompt,
        use_loop=use_loop,
    )


async def _invoke_remote(
    ref: RemoteAgentRef,
    prompt: str,
) -> str:
    """Invoke a remote A2A agent over HTTP."""
    import uuid

    from obscura.integrations.a2a.client import A2AClient
    from obscura.integrations.a2a.types import A2AMessage, TextPart

    client = A2AClient(ref.url)
    try:
        await client.connect()
        message = A2AMessage(
            role="user",
            messageId=uuid.uuid4().hex,
            parts=[TextPart(text=prompt)],
        )
        task = await client.send_message(message)
        # Extract text from the completed task.
        return _extract_task_text(task)
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


async def _invoke_unix_socket(
    ref: UnixSocketAgentRef,
    prompt: str,
) -> str:
    """Invoke an agent over a Unix domain socket."""
    from obscura.integrations.a2a.transports.unix_socket import (
        UnixSocketA2AClient,
    )

    client = UnixSocketA2AClient(ref.socket_path)
    try:
        await client.connect()
        return await client.send_message(prompt)
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


def _extract_task_text(task: Any) -> str:
    """Pull text content from an A2A Task."""
    parts: list[str] = []
    if hasattr(task, "artifacts"):
        for artifact in task.artifacts:
            for part in artifact.parts:
                if hasattr(part, "text"):
                    parts.append(part.text)
    if parts:
        return "\n".join(parts)
    # Fallback: check the status message.
    if hasattr(task, "status") and task.status.message:
        for part in task.status.message.parts:
            if hasattr(part, "text"):
                parts.append(part.text)
    return "\n".join(parts) if parts else str(task)


# ---------------------------------------------------------------------------
# Context injection for prompt assembly
# ---------------------------------------------------------------------------


def build_agent_cards_section(
    peer_registry: PeerRegistry,
    remote_refs: list[RemoteAgentRef] | None = None,
    unix_socket_refs: list[UnixSocketAgentRef] | None = None,
) -> str:
    """Generate a text block describing available agents for delegation.

    Injected into the prompt assembler's ``context_instructions`` section
    so the LLM understands which agents are available and when to use them.
    """
    lines: list[str] = ["## Available Agents for Delegation", ""]

    local_refs = peer_registry.discover()
    if not local_refs and not remote_refs and not unix_socket_refs:
        lines.append("No agents are currently available for delegation.")
        return "\n".join(lines)

    for ref in local_refs:
        caps = ", ".join(ref.capabilities) if ref.capabilities else "general"
        lines.append(
            f"- **{ref.name}** (local, {ref.model}): {caps} [status: {ref.status}]",
        )

    for ref in remote_refs or []:
        name = ref.name or ref.url
        desc = ref.description or "remote agent"
        lines.append(f"- **{name}** (remote): {desc}")

    for ref in unix_socket_refs or []:
        if ref.status == "available":
            name = ref.name or ref.socket_path
            desc = ref.description or "unix socket agent"
            lines.append(f"- **{name}** (unix_socket): {desc}")

    lines.append("")
    lines.append(
        "Use the `delegate_to_agent` tool to send tasks to these agents. "
        "Only delegate when the target agent has relevant capabilities.",
    )
    return "\n".join(lines)
