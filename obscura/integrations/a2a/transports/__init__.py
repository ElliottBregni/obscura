# A2A transport adapters: JSON-RPC, REST, SSE, gRPC, Unix socket.

from obscura.integrations.a2a.transports.jsonrpc import create_jsonrpc_router
from obscura.integrations.a2a.transports.rest import (
    create_rest_router,
    create_wellknown_router,
)
from obscura.integrations.a2a.transports.sse import create_sse_router
from obscura.integrations.a2a.transports.unix_socket import (
    UnixSocketA2AClient,
    start_unix_socket_server,
    stop_unix_socket_server,
)

__all__ = [
    "create_jsonrpc_router",
    "create_rest_router",
    "create_wellknown_router",
    "create_sse_router",
    "UnixSocketA2AClient",
    "start_unix_socket_server",
    "stop_unix_socket_server",
]
