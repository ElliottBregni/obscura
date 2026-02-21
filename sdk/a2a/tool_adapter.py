"""
sdk.a2a.tool_adapter — Register remote A2A agents as Obscura tools.

Wraps an ``A2AClient`` as a ``ToolSpec`` so any Obscura agent can invoke
remote A2A agents through the standard tool-calling interface.

Pattern mirrors ``sdk/mcp/tools.py:mcp_tool_to_obscura()``.

Usage::

    from sdk.a2a.tool_adapter import register_remote_agent_as_tool

    registry = ToolRegistry()
    register_remote_agent_as_tool(
        registry=registry,
        client=a2a_client,
        tool_name="support-agent",
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sdk.a2a.client import A2AClient
from sdk.internal.tools import ToolRegistry
from sdk.internal.types import ToolSpec

logger = logging.getLogger(__name__)


def register_remote_agent_as_tool(
    registry: ToolRegistry,
    client: A2AClient,
    *,
    tool_name: str | None = None,
    description: str | None = None,
    context_id: str | None = None,
    blocking: bool = True,
) -> ToolSpec:
    """Register a remote A2A agent as a local tool.

    Parameters
    ----------
    registry:
        The ToolRegistry to register the tool in.
    client:
        Connected A2AClient for the remote agent.
    tool_name:
        Override the tool name (default: agent card name or URL).
    description:
        Override the tool description.
    context_id:
        Fixed context ID for multi-turn conversations.
    blocking:
        Whether to wait for task completion.

    Returns
    -------
    ToolSpec:
        The registered tool spec.
    """
    card = client.agent_card
    name = tool_name or (card.name if card else client.base_url.split("//")[-1])
    desc = description or (card.description if card else f"Remote A2A agent at {client.base_url}")

    # Clean name for tool registration (no spaces, lowercase)
    safe_name = name.lower().replace(" ", "_").replace("-", "_")

    async def handler(message: str = "", **kwargs: Any) -> str:
        """Invoke the remote A2A agent."""
        try:
            task = await client.send_message(
                message or json.dumps(kwargs),
                context_id=context_id,
                blocking=blocking,
            )

            # Extract result from artifacts
            results = []
            for artifact in task.artifacts:
                for part in artifact.parts:
                    if hasattr(part, "text"):
                        results.append(part.text)
                    elif hasattr(part, "data"):
                        results.append(json.dumps(part.data))

            if results:
                return "\n".join(results)

            return f"Task {task.id} completed with status: {task.status.state.value}"

        except Exception as e:
            logger.error("A2A tool call failed: %s", e)
            return f"Error calling remote agent: {e}"

    spec = ToolSpec(
        name=safe_name,
        description=desc,
        parameters={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "Message to send to the remote agent",
                },
            },
            "required": ["message"],
        },
        handler=handler,
    )

    registry.register(spec)
    return spec


def register_agents_from_urls(
    registry: ToolRegistry,
    urls: list[str],
    *,
    auth_token: str | None = None,
) -> list[ToolSpec]:
    """Discover and register multiple remote agents as tools.

    Synchronous convenience wrapper that discovers agent cards
    and registers each as a tool.

    Parameters
    ----------
    registry:
        The ToolRegistry to register tools in.
    urls:
        List of A2A server URLs.
    auth_token:
        Optional shared auth token.

    Returns
    -------
    list[ToolSpec]:
        The registered tool specs.
    """
    specs: list[ToolSpec] = []

    async def _register_all() -> None:
        for url in urls:
            client = A2AClient(url, auth_token=auth_token)
            await client.connect()
            try:
                await client.discover()
                spec = register_remote_agent_as_tool(registry, client)
                specs.append(spec)
            except Exception as e:
                logger.error("Failed to register agent at %s: %s", url, e)
                await client.disconnect()

    asyncio.get_event_loop().run_until_complete(_register_all())
    return specs
