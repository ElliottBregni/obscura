"""JSON-RPC and MCP boundary models.

Boundary in the §2.7 sense: every byte that crosses a wire is parsed into one
of these models on ingress and serialized via :meth:`pydantic.BaseModel.model_dump`
on egress. Internal handlers never see the raw dict.

The ``method`` field stays a plain ``str`` rather than ``MCPMethod``: the
canonical enum lives in :mod:`obscura.core.enums.protocol` and downstream
dispatch matches against it after parsing, but the boundary stays
permissive so unknown methods produce a typed ``METHOD_NOT_FOUND`` error
instead of a Pydantic validation error.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from obscura.core.models._base import BoundaryModel


class JSONRPCError(BoundaryModel):
    """JSON-RPC 2.0 error object."""

    code: int
    message: str
    data: Any | None = None


class JSONRPCRequest(BoundaryModel):
    """JSON-RPC 2.0 request envelope.

    A request without an ``id`` is a notification.
    """

    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    method: str
    params: Mapping[str, Any] | None = None


class JSONRPCResponse(BoundaryModel):
    """JSON-RPC 2.0 response envelope.

    Exactly one of ``result`` or ``error`` should be populated, but the
    boundary tolerates either ordering — downstream code disambiguates.
    """

    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    result: Any | None = None
    error: JSONRPCError | None = None


class MCPInitializeParams(BoundaryModel):
    """``params`` for :data:`MCPMethod.INITIALIZE`."""

    protocolVersion: str
    capabilities: Mapping[str, Any] = Field(default_factory=dict)
    clientInfo: Mapping[str, str] = Field(default_factory=dict)


class MCPListToolsParams(BoundaryModel):
    """``params`` for :data:`MCPMethod.TOOLS_LIST`."""

    cursor: str | None = None


class MCPCallToolParams(BoundaryModel):
    """``params`` for :data:`MCPMethod.TOOLS_CALL`."""

    name: str
    arguments: Mapping[str, Any] = Field(default_factory=dict)


class MCPListResourcesParams(BoundaryModel):
    """``params`` for :data:`MCPMethod.RESOURCES_LIST`."""

    cursor: str | None = None


class MCPReadResourceParams(BoundaryModel):
    """``params`` for :data:`MCPMethod.RESOURCES_READ`."""

    uri: str


class MCPListPromptsParams(BoundaryModel):
    """``params`` for :data:`MCPMethod.PROMPTS_LIST`."""

    cursor: str | None = None


class MCPGetPromptParams(BoundaryModel):
    """``params`` for :data:`MCPMethod.PROMPTS_GET`."""

    name: str
    arguments: Mapping[str, str] | None = None


__all__ = [
    "JSONRPCError",
    "JSONRPCRequest",
    "JSONRPCResponse",
    "MCPCallToolParams",
    "MCPGetPromptParams",
    "MCPInitializeParams",
    "MCPListPromptsParams",
    "MCPListResourcesParams",
    "MCPListToolsParams",
    "MCPReadResourceParams",
]
