"""Protocol enums — MCP and A2A wire-format identifiers.

Every value here is a byte-for-byte external wire string. Member names use
Python-friendly UPPER_SNAKE; values preserve the original separators
(``/`` for MCP methods, ``-`` for A2A status events, ``_`` for internal
notification names) so JSON envelopes round-trip unchanged.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class MCPMethod(StrEnum):
    """MCP JSON-RPC method names."""

    INITIALIZE = "initialize"
    INITIALIZED = "notifications/initialized"
    PING = "ping"

    TOOLS_LIST = "tools/list"
    TOOLS_CALL = "tools/call"

    RESOURCES_LIST = "resources/list"
    RESOURCES_READ = "resources/read"
    RESOURCES_SUBSCRIBE = "resources/subscribe"
    RESOURCES_UNSUBSCRIBE = "resources/unsubscribe"

    PROMPTS_LIST = "prompts/list"
    PROMPTS_GET = "prompts/get"

    ROOTS_LIST = "roots/list"

    SAMPLING_CREATE_MESSAGE = "sampling/createMessage"


class MCPLogLevel(StrEnum):
    """Severity levels accepted on MCP ``notifications/message`` payloads."""

    DEBUG = "debug"
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"
    ALERT = "alert"
    EMERGENCY = "emergency"


class MCPTransport(StrEnum):
    """Supported MCP connection transports."""

    STDIO = "stdio"
    SSE = "sse"


class JSONRPCErrorCode(IntEnum):
    """Standard JSON-RPC 2.0 error codes (negative integers)."""

    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603


class A2APartKind(StrEnum):
    """A2A message part discriminator values."""

    TEXT = "text"
    FILE = "file"
    DATA = "data"


class A2ARole(StrEnum):
    """A2A message author roles."""

    USER = "user"
    AGENT = "agent"


class A2ATaskMessageKind(StrEnum):
    """Discriminator values for A2A task and streaming envelopes."""

    TASK = "task"
    STATUS_UPDATE = "status-update"
    ARTIFACT_UPDATE = "artifact-update"


class A2ATaskState(StrEnum):
    """A2A task lifecycle states."""

    PENDING = "pending"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    AUTH_REQUIRED = "auth-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"


class A2AMethod(StrEnum):
    """A2A JSON-RPC method names."""

    MESSAGE_SEND = "message/send"
    MESSAGE_STREAM = "message/stream"
    TASKS_GET = "tasks/get"
    TASKS_LIST = "tasks/list"
    TASKS_CANCEL = "tasks/cancel"
    TASKS_SUBSCRIBE = "tasks/subscribe"
    PUSH_CONFIG_CREATE = "tasks/pushNotificationConfig/create"
    PUSH_CONFIG_GET = "tasks/pushNotificationConfig/get"
    PUSH_CONFIG_LIST = "tasks/pushNotificationConfig/list"
    PUSH_CONFIG_DELETE = "tasks/pushNotificationConfig/delete"
    AGENT_CARD = "agent/authenticatedExtendedCard"
