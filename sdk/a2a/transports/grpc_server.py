"""
sdk.a2a.transports.grpc_server — gRPC transport for A2A.

Uses a JSON-over-gRPC approach: messages are serialized as JSON strings
in generic request/response wrappers. This avoids a hard dependency on
``grpcio-tools`` at development time while providing a fully functional
gRPC transport.

If protobuf-generated stubs are available (from ``sdk.a2a.proto``),
they can be used instead for native proto serialization.

Usage::

    from sdk.a2a.transports.grpc_server import start_grpc_server

    server = await start_grpc_server(service, port=50051)
    # ... later ...
    await server.stop(grace=5)
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from sdk.a2a.service import A2AService
from sdk.a2a.types import (
    A2AError,
    A2AMessage,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)

logger = logging.getLogger(__name__)


class A2AServicer:
    """gRPC servicer that delegates to A2AService.

    Implements the A2A gRPC service methods using JSON serialization
    over generic gRPC request/response messages.
    """

    def __init__(self, service: A2AService) -> None:
        self._service = service

    async def SendMessage(self, request_json: str) -> str:
        """Handle SendMessage RPC."""
        params = json.loads(request_json)
        message = A2AMessage.model_validate(params.get("message", {}))
        task = await self._service.message_send(
            message,
            context_id=params.get("contextId"),
            task_id=params.get("taskId"),
            blocking=params.get("blocking", True),
        )
        return task.model_dump_json()

    async def StreamMessage(self, request_json: str) -> AsyncIterator[str]:
        """Handle StreamMessage RPC — yields JSON events."""
        params = json.loads(request_json)
        message = A2AMessage.model_validate(params.get("message", {}))
        async for event in self._service.message_stream(
            message, context_id=params.get("contextId")
        ):
            yield event.model_dump_json()

    async def GetTask(self, request_json: str) -> str:
        """Handle GetTask RPC."""
        params = json.loads(request_json)
        task_id = params.get("taskId", "")
        task = await self._service.tasks_get(task_id)
        if task is None:
            from sdk.a2a.types import TaskNotFoundError
            raise TaskNotFoundError(task_id)
        return task.model_dump_json()

    async def ListTasks(self, request_json: str) -> str:
        """Handle ListTasks RPC."""
        params = json.loads(request_json)
        tasks, cursor = await self._service.tasks_list(
            context_id=params.get("contextId"),
            state=TaskState(params["state"]) if "state" in params else None,
            cursor=params.get("cursor"),
            limit=params.get("limit", 20),
        )
        result: dict[str, Any] = {
            "tasks": [json.loads(t.model_dump_json()) for t in tasks],
        }
        if cursor:
            result["nextCursor"] = cursor
        return json.dumps(result)

    async def CancelTask(self, request_json: str) -> str:
        """Handle CancelTask RPC."""
        params = json.loads(request_json)
        task_id = params.get("taskId", "")
        task = await self._service.tasks_cancel(task_id)
        return task.model_dump_json()

    async def SubscribeToTask(self, request_json: str) -> AsyncIterator[str]:
        """Handle SubscribeToTask RPC — yields JSON events."""
        params = json.loads(request_json)
        task_id = params.get("taskId", "")
        async for event in self._service.tasks_subscribe(task_id):
            yield event.model_dump_json()

    async def GetAgentCard(self, request_json: str = "{}") -> str:
        """Handle GetAgentCard RPC."""
        card = self._service.get_agent_card()
        return card.model_dump_json()


async def start_grpc_server(
    service: A2AService,
    port: int = 50051,
    *,
    enable_reflection: bool = True,
) -> Any:
    """Start a gRPC server serving the A2A protocol.

    Uses ``grpc.aio`` for async operation. Returns the server instance
    which can be stopped with ``await server.stop(grace=5)``.

    Parameters
    ----------
    service:
        The A2AService to delegate RPCs to.
    port:
        Port to listen on.
    enable_reflection:
        Enable gRPC reflection for discovery (requires ``grpcio-reflection``).
    """
    try:
        import grpc
        import grpc.aio
    except ImportError:
        raise ImportError(
            "grpcio is required for gRPC transport. "
            "Install with: pip install 'obscura[a2a]'"
        )

    servicer = A2AServicer(service)

    # Build generic handlers that use JSON serialization
    server = grpc.aio.server()

    # Register a generic service using add_generic_rpc_handlers
    method_handlers = {
        "/a2a.A2AService/SendMessage": grpc.unary_unary_rpc_method_handler(
            _wrap_unary(servicer.SendMessage),
            request_deserializer=lambda b: b.decode("utf-8"),
            response_serializer=lambda s: s.encode("utf-8"),
        ),
        "/a2a.A2AService/GetTask": grpc.unary_unary_rpc_method_handler(
            _wrap_unary(servicer.GetTask),
            request_deserializer=lambda b: b.decode("utf-8"),
            response_serializer=lambda s: s.encode("utf-8"),
        ),
        "/a2a.A2AService/ListTasks": grpc.unary_unary_rpc_method_handler(
            _wrap_unary(servicer.ListTasks),
            request_deserializer=lambda b: b.decode("utf-8"),
            response_serializer=lambda s: s.encode("utf-8"),
        ),
        "/a2a.A2AService/CancelTask": grpc.unary_unary_rpc_method_handler(
            _wrap_unary(servicer.CancelTask),
            request_deserializer=lambda b: b.decode("utf-8"),
            response_serializer=lambda s: s.encode("utf-8"),
        ),
        "/a2a.A2AService/GetAgentCard": grpc.unary_unary_rpc_method_handler(
            _wrap_unary(servicer.GetAgentCard),
            request_deserializer=lambda b: b.decode("utf-8"),
            response_serializer=lambda s: s.encode("utf-8"),
        ),
        "/a2a.A2AService/StreamMessage": grpc.unary_stream_rpc_method_handler(
            _wrap_stream(servicer.StreamMessage),
            request_deserializer=lambda b: b.decode("utf-8"),
            response_serializer=lambda s: s.encode("utf-8"),
        ),
        "/a2a.A2AService/SubscribeToTask": grpc.unary_stream_rpc_method_handler(
            _wrap_stream(servicer.SubscribeToTask),
            request_deserializer=lambda b: b.decode("utf-8"),
            response_serializer=lambda s: s.encode("utf-8"),
        ),
    }

    handler = grpc.method_service_handler(None, method_handlers)
    server.add_generic_rpc_handlers([handler])

    server.add_insecure_port(f"[::]:{port}")
    await server.start()
    logger.info("A2A gRPC server started on port %d", port)

    return server


def _wrap_unary(fn):
    """Wrap an async servicer method for grpc.aio unary handler."""
    async def handler(request, context):
        try:
            return await fn(request)
        except A2AError as e:
            import grpc
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(json.dumps({"code": e.code, "message": e.message}))
            return ""
        except Exception as e:
            import grpc
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return ""
    return handler


def _wrap_stream(fn):
    """Wrap an async generator servicer method for grpc.aio stream handler."""
    async def handler(request, context):
        try:
            async for item in fn(request):
                yield item
        except A2AError as e:
            import grpc
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(json.dumps({"code": e.code, "message": e.message}))
        except Exception as e:
            import grpc
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
    return handler
