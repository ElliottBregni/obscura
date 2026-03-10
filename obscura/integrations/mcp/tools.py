"""
obscura.mcp.tools — Tool conversion between MCP <-> Obscura.

Converts between Obscura ToolSpec and MCP tool definitions,
and handles execution of MCP tools.
"""

import json
import logging
from typing import Any, Awaitable, Callable, cast

from obscura.core.types import ToolSpec
from obscura.integrations.mcp.types import (
    MCPError,
    MCPErrorCode,
    MCPTool,
    MCPToolResult,
    ObscuraMCPToolContext,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Obscura -> MCP Tool Conversion
# ---------------------------------------------------------------------------


def obscura_tool_to_mcp(tool: ToolSpec) -> MCPTool:
    """
    Convert an Obscura ToolSpec to an MCP Tool definition.

    Args:
        tool: Obscura ToolSpec to convert

    Returns:
        MCPTool definition for MCP protocol
    """
    # Convert parameters to JSON Schema format
    schema: dict[str, Any] = {
        "type": "object",
        "properties": tool.parameters.get("properties", {}),
        "required": tool.parameters.get("required", []),
    }

    return MCPTool(
        name=tool.name,
        description=tool.description,
        inputSchema=schema,
    )


def mcp_tool_to_obscura(
    tool: MCPTool, execute_fn: Callable[[dict[str, Any]], Awaitable[Any]]
) -> ToolSpec:
    """
    Convert an MCP Tool definition to an Obscura ToolSpec.

    Args:
        tool: MCP Tool definition
        execute_fn: Function to execute the tool

    Returns:
        Obscura ToolSpec
    """
    # Normalise the MCP input schema so it always has "type": "object"
    # — some MCP servers omit it, which breaks OpenAI-compatible backends.
    schema = dict(tool.inputSchema) if tool.inputSchema else {}
    if schema.get("type") != "object":
        schema["type"] = "object"
        schema.setdefault("properties", {})
    return ToolSpec(
        name=tool.name,
        description=tool.description,
        parameters=schema,
        handler=execute_fn,
    )


# ---------------------------------------------------------------------------
# Tool Result Conversion
# ---------------------------------------------------------------------------


def obscura_result_to_mcp(result: object | None) -> MCPToolResult:
    """
    Convert an Obscura tool execution result to MCP ToolResult.

    Args:
        result: Result from Obscura tool execution

    Returns:
        MCPToolResult for MCP protocol
    """
    if result is None:
        return MCPToolResult(content=[])

    if isinstance(result, dict):
        result_dict = cast(dict[str, Any], result)
        if "error" in result_dict:
            return MCPToolResult(
                content=[{"type": "text", "text": str(result_dict["error"])}],
                isError=True,
            )
        return MCPToolResult(
            content=[
                {
                    "type": "text",
                    "text": json.dumps(result_dict, indent=2, default=str),
                }
            ],
        )

    if isinstance(result, str):
        return MCPToolResult(
            content=[{"type": "text", "text": result}],
        )

    if isinstance(result, list):
        result_list = cast(list[Any], result)
        content: list[dict[str, Any]] = []
        for item in result_list:
            if isinstance(item, str):
                content.append({"type": "text", "text": item})
            elif isinstance(item, dict):
                item_dict = cast(dict[str, Any], item)
                if item_dict.get("type") in ["text", "image", "resource"]:
                    content.append(item_dict)
                else:
                    content.append(
                        {
                            "type": "text",
                            "text": json.dumps(item_dict, indent=2),
                        }
                    )
            else:
                content.append({"type": "text", "text": str(item)})
        return MCPToolResult(content=content)

    # Default: convert to JSON
    try:
        payload = json.dumps(result, indent=2, default=str)
    except Exception as e:
        logger.warning(f"Failed to serialize result to JSON: {e}")
        payload = str(result)

    return MCPToolResult(content=[{"type": "text", "text": payload}])


def mcp_result_to_obscura(result: MCPToolResult) -> Any:
    """
    Convert an MCP ToolResult to an Obscura-friendly result.

    Args:
        result: MCP ToolResult

    Returns:
        Result suitable for Obscura
    """
    if result.isError:
        return {"error": result.content}

    if not result.content:
        return None

    # Extract text from content
    texts: list[str] = []
    for item in result.content:
        if item.get("type") == "text":
            texts.append(str(item.get("text", "")))
        elif item.get("type") == "image":
            # Handle image content
            texts.append(f"[Image: {item.get('mimeType', 'unknown')}]")
        elif item.get("type") == "resource":
            # Handle resource reference
            resource: dict[str, Any] = item.get("resource", {})
            texts.append(f"[Resource: {resource.get('uri', 'unknown')}]")
        else:
            texts.append(str(item))

    if len(texts) == 1:
        return texts[0]
    return texts


# ---------------------------------------------------------------------------
# Obscura MCP Tools Registry
# ---------------------------------------------------------------------------


class ObscuraMCPToolRegistry:
    """
    Registry for Obscura-specific MCP tools.

    These tools expose Obscura functionality via the MCP protocol.
    """

    def __init__(self) -> None:
        self._tools: dict[
            str, Callable[[ObscuraMCPToolContext, dict[str, Any]], Awaitable[Any]]
        ] = {}
        self._schemas: dict[str, dict[str, Any]] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[[ObscuraMCPToolContext, dict[str, Any]], Awaitable[Any]],
    ) -> None:
        """
        Register an Obscura MCP tool.

        Args:
            name: Tool name
            description: Tool description
            parameters: JSON Schema for parameters
            handler: Async function to handle tool calls
        """
        self._tools[name] = handler
        self._schemas[name] = {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": parameters.get("properties", {}),
                "required": parameters.get("required", []),
            },
        }
        logger.debug(f"Registered Obscura MCP tool: {name}")

    def get_tool(
        self, name: str
    ) -> Callable[[ObscuraMCPToolContext, dict[str, Any]], Awaitable[Any]] | None:
        """Get a tool handler by name."""
        return self._tools.get(name)

    def list_tools(self) -> list[MCPTool]:
        """List all registered tools as MCP Tool definitions."""
        return [
            MCPTool(
                name=schema["name"],
                description=schema["description"],
                inputSchema=schema["inputSchema"],
            )
            for schema in self._schemas.values()
        ]

    async def execute(
        self,
        name: str,
        context: ObscuraMCPToolContext,
        arguments: dict[str, Any],
    ) -> MCPToolResult:
        """
        Execute a tool by name.

        Args:
            name: Tool name
            context: Tool execution context
            arguments: Tool arguments

        Returns:
            MCP ToolResult
        """
        handler = self._tools.get(name)
        if handler is None:
            raise MCPError(
                code=MCPErrorCode.TOOL_NOT_FOUND.value,
                message=f"Tool not found: {name}",
            )

        try:
            result = await handler(context, arguments)
            return obscura_result_to_mcp(result)
        except Exception as e:
            logger.exception(f"Tool execution failed: {name}")
            raise MCPError(
                code=MCPErrorCode.TOOL_EXECUTION_ERROR.value,
                message=f"Tool execution failed: {str(e)}",
                data={"tool": name, "error": str(e)},
            )


# Global registry instance
_obscura_mcp_tools = ObscuraMCPToolRegistry()


def get_obscura_mcp_registry() -> ObscuraMCPToolRegistry:
    """Get the global Obscura MCP tool registry."""
    return _obscura_mcp_tools


# ---------------------------------------------------------------------------
# Tool Execution Context Management
# ---------------------------------------------------------------------------


def create_tool_context(
    user_id: str,
    agent_id: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
) -> ObscuraMCPToolContext:
    """
    Create a tool execution context.

    Args:
        user_id: User ID
        agent_id: Optional agent ID
        session_id: Optional session ID
        request_id: Optional request ID

    Returns:
        Tool execution context
    """
    return ObscuraMCPToolContext(
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# JSON Schema Helpers
# ---------------------------------------------------------------------------


def create_string_property(
    description: str,
    enum: list[str] | None = None,
    default: str | None = None,
) -> dict[str, Any]:
    """Create a string property schema."""
    prop: dict[str, Any] = {"type": "string", "description": description}
    if enum:
        prop["enum"] = enum
    if default is not None:
        prop["default"] = default
    return prop


def create_integer_property(
    description: str,
    minimum: int | None = None,
    maximum: int | None = None,
    default: int | None = None,
) -> dict[str, Any]:
    """Create an integer property schema."""
    prop: dict[str, Any] = {"type": "integer", "description": description}
    if minimum is not None:
        prop["minimum"] = minimum
    if maximum is not None:
        prop["maximum"] = maximum
    if default is not None:
        prop["default"] = default
    return prop


def create_boolean_property(
    description: str,
    default: bool = False,
) -> dict[str, Any]:
    """Create a boolean property schema."""
    return {"type": "boolean", "description": description, "default": default}


def create_array_property(
    description: str,
    items: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an array property schema."""
    prop: dict[str, Any] = {"type": "array", "description": description}
    if items:
        prop["items"] = items
    return prop


def create_object_property(
    description: str,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an object property schema."""
    prop: dict[str, Any] = {"type": "object", "description": description}
    if properties:
        prop["properties"] = properties
    return prop
