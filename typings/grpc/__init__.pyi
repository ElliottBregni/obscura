from typing import Any, Callable

class StatusCode:
    OK: StatusCode
    INTERNAL: StatusCode
    INVALID_ARGUMENT: StatusCode
    NOT_FOUND: StatusCode

def unary_unary_rpc_method_handler(
    behavior: Callable[..., Any],
    request_deserializer: Callable[..., Any] | None = None,
    response_serializer: Callable[..., Any] | None = None,
) -> Any: ...
def unary_stream_rpc_method_handler(
    behavior: Callable[..., Any],
    request_deserializer: Callable[..., Any] | None = None,
    response_serializer: Callable[..., Any] | None = None,
) -> Any: ...
def method_service_handler(
    service_name: str | None,
    method_handlers: dict[str, Any],
) -> Any: ...
