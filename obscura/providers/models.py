"""Typed payload models for OpenAI-compatible backends."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """Configuration for an MCP server passed to backends."""

    transport: str
    command: list[str] | None = None
    env: dict[str, str] | None = None
    working_dir: str | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MCPServerConfig":
        return cls(
            transport=str(data.get("transport", "")),
            command=list(data["command"])
            if "command" in data and data["command"] is not None
            else None,
            env=dict(data["env"])
            if "env" in data and data["env"] is not None
            else None,
            working_dir=str(data["working_dir"])
            if "working_dir" in data and data["working_dir"] is not None
            else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport": self.transport,
            "command": self.command,
            "env": self.env,
            "working_dir": self.working_dir,
        }


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """A message in a conversation history.

    Extended to support tool call and tool result messages for correct
    multi-turn conversation persistence.
    """

    role: str
    content: str | list[dict[str, Any]] = ""
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls is not None:
            d["tool_calls"] = self.tool_calls
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            d["name"] = self.name
        return d


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    object: str

    @classmethod
    def from_openai(cls, obj: Any) -> "ModelInfo":
        return cls(id=str(obj.id), object=str(getattr(obj, "object", "model")))

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "object": self.object}


@dataclass(frozen=True, slots=True)
class CompletionParams:
    """Subset of OpenAI chat completion params we allow passthrough."""

    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: list[str] | str | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    seed: int | None = None
    response_format: Any | None = None
    tool_choice: Any | None = None

    @classmethod
    def from_kwargs(cls, kwargs: Mapping[str, Any]) -> "CompletionParams":
        valid_keys = {
            "temperature",
            "top_p",
            "max_tokens",
            "stop",
            "frequency_penalty",
            "presence_penalty",
            "seed",
            "response_format",
            "tool_choice",
        }
        filtered: dict[str, Any] = {k: v for k, v in kwargs.items() if k in valid_keys}
        return cls(**filtered)

    def to_dict(self) -> dict[str, Any]:
        # dataclass with slots has no __dict__, so use asdict and drop Nones
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """Unified request object for backend ``send`` / ``stream``.

    Callers can build this explicitly or continue using ``prompt + **kwargs``.
    Backends extract what they need via ``from_kwargs()``.
    """

    prompt: str = ""
    messages: list[Any] | None = None
    params: CompletionParams | None = None
    tool_choice: Any | None = None
    session: Any | None = None
    metadata: dict[str, Any] | None = None
    timeout_s: float | None = None

    @classmethod
    def from_kwargs(cls, prompt: str, kwargs: Mapping[str, Any]) -> "AgentRequest":
        """Extract an ``AgentRequest`` from the legacy ``prompt + **kwargs`` pattern."""
        return cls(
            prompt=prompt,
            messages=kwargs.get("messages"),
            params=CompletionParams.from_kwargs(kwargs),
            tool_choice=kwargs.get("tool_choice"),
            session=kwargs.get("session"),
            metadata=kwargs.get("request_metadata"),
            timeout_s=kwargs.get("timeout_s"),
        )


@dataclass(frozen=True, slots=True)
class ToolCallDefinition:
    name: str
    description: str
    parameters: Mapping[str, Any]

    def to_openai_function(self) -> dict[str, Any]:
        params = dict(self.parameters)
        # OpenAI requires parameters.type == "object".  Normalise schemas
        # that arrive without it (common with MCP tool definitions).
        if params.get("type") != "object":
            params["type"] = "object"
            params.setdefault("properties", {})
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params,
            },
        }
