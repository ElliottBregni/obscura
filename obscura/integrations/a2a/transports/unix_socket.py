"""
obscura.a2a.transports.unix_socket — Unix domain socket transport for A2A.

Provides a lightweight, zero-network-overhead transport for local
agent-to-agent communication.  Uses NDJSON (newline-delimited JSON)
over ``asyncio`` Unix streams.

Server usage::

    from obscura.integrations.a2a.transports.unix_socket import start_unix_socket_server

    server = await start_unix_socket_server(service, "/tmp/obscura-a2a.sock")
    # ... later ...
    server.close()
    await server.wait_closed()

Client usage::

    from obscura.integrations.a2a.transports.unix_socket import UnixSocketA2AClient

    client = UnixSocketA2AClient("/tmp/obscura-a2a.sock")
    await client.connect()
    result = await client.send_message("Fix the auth bug")
    await client.disconnect()
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

from obscura.integrations.a2a.service import A2AService
from obscura.integrations.a2a.types import (
    A2AError,
    A2AMessage,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Servicer — dispatches JSON requests to A2AService
# ---------------------------------------------------------------------------


class UnixSocketServicer:
    """Thin adapter that dispatches NDJSON requests to an A2AService."""

    def __init__(self, service: A2AService) -> None:
        self._service = service

    async def handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single client connection (one request per line)."""
        peer = writer.get_extra_info("peername") or "unix"
        logger.debug("Unix socket connection from %s", peer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    request = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError as exc:
                    await _write_json(writer, {
                        "error": {"code": -32700, "message": f"Parse error: {exc}"}
                    })
                    continue

                method = request.get("method", "")
                params = request.get("params", {})

                try:
                    if method in ("SendMessage", "message/send"):
                        result = await self._handle_send_message(params)
                        await _write_json(writer, {"result": result})
                    elif method in ("StreamMessage", "message/stream"):
                        async for event in self._handle_stream_message(params):
                            await _write_json(writer, {"event": event})
                        await _write_json(writer, {"done": True})
                    elif method in ("GetTask", "tasks/get"):
                        result = await self._handle_get_task(params)
                        await _write_json(writer, {"result": result})
                    elif method in ("ListTasks", "tasks/list"):
                        result = await self._handle_list_tasks(params)
                        await _write_json(writer, {"result": result})
                    elif method in ("CancelTask", "tasks/cancel"):
                        result = await self._handle_cancel_task(params)
                        await _write_json(writer, {"result": result})
                    elif method in ("GetAgentCard", "agent/card"):
                        card = self._service.get_agent_card()
                        await _write_json(writer, {
                            "result": json.loads(card.model_dump_json())
                        })
                    else:
                        await _write_json(writer, {
                            "error": {
                                "code": -32601,
                                "message": f"Unknown method: {method}",
                            }
                        })
                except A2AError as exc:
                    await _write_json(writer, {
                        "error": {"code": exc.code, "message": exc.message}
                    })
                except Exception as exc:
                    logger.exception("Error handling method %s", method)
                    await _write_json(writer, {
                        "error": {"code": -32000, "message": str(exc)}
                    })
        except asyncio.IncompleteReadError:
            pass
        except ConnectionResetError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            logger.debug("Unix socket connection closed: %s", peer)

    async def _handle_send_message(self, params: dict[str, Any]) -> Any:
        message = A2AMessage.model_validate(params.get("message", {}))
        task = await self._service.message_send(
            message,
            context_id=params.get("contextId"),
            task_id=params.get("taskId"),
            blocking=params.get("blocking", True),
        )
        return json.loads(task.model_dump_json())

    async def _handle_stream_message(
        self, params: dict[str, Any]
    ) -> Any:
        message = A2AMessage.model_validate(params.get("message", {}))
        async for event in self._service.message_stream(
            message, context_id=params.get("contextId")
        ):
            yield json.loads(event.model_dump_json())

    async def _handle_get_task(self, params: dict[str, Any]) -> Any:
        task_id = params.get("taskId", "")
        task = await self._service.tasks_get(task_id)
        if task is None:
            from obscura.integrations.a2a.types import TaskNotFoundError

            raise TaskNotFoundError(task_id)
        return json.loads(task.model_dump_json())

    async def _handle_list_tasks(self, params: dict[str, Any]) -> Any:
        from obscura.integrations.a2a.types import TaskState

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
        return result

    async def _handle_cancel_task(self, params: dict[str, Any]) -> Any:
        task_id = params.get("taskId", "")
        task = await self._service.tasks_cancel(task_id)
        return json.loads(task.model_dump_json())


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


async def start_unix_socket_server(
    service: A2AService,
    socket_path: str = "/tmp/obscura-a2a.sock",
) -> asyncio.Server:
    """Start a Unix domain socket server for the A2A protocol.

    Parameters
    ----------
    service:
        The A2AService to delegate requests to.
    socket_path:
        Path for the Unix socket file.

    Returns
    -------
    asyncio.Server
        The running server. Call ``server.close()`` and
        ``await server.wait_closed()`` to stop.
    """
    # Remove stale socket file if it exists.
    if os.path.exists(socket_path):
        os.unlink(socket_path)

    servicer = UnixSocketServicer(service)
    server = await asyncio.start_unix_server(
        servicer.handle_connection,
        path=socket_path,
    )
    logger.info("A2A Unix socket server started at %s", socket_path)
    return server


async def stop_unix_socket_server(
    server: asyncio.Server,
    socket_path: str,
) -> None:
    """Stop the Unix socket server and clean up the socket file."""
    server.close()
    await server.wait_closed()
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    logger.info("A2A Unix socket server stopped, removed %s", socket_path)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class UnixSocketA2AClient:
    """Client for invoking A2A agents over a Unix domain socket.

    Uses the same NDJSON protocol as the server: send a JSON line with
    ``method`` and ``params``, receive a JSON line with ``result`` or
    ``error``.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        """Open a connection to the Unix socket server."""
        self._reader, self._writer = await asyncio.open_unix_connection(
            self._socket_path
        )

    async def disconnect(self) -> None:
        """Close the connection."""
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

    async def send_message(
        self,
        prompt: str,
        *,
        context_id: str | None = None,
        blocking: bool = True,
    ) -> str:
        """Send a message and return the text result.

        Parameters
        ----------
        prompt:
            The text prompt to send.
        context_id:
            Optional conversation context ID.
        blocking:
            Whether to wait for task completion.

        Returns
        -------
        str
            The text content from the task result.
        """
        if self._reader is None or self._writer is None:
            raise RuntimeError("Not connected — call connect() first")

        message = {
            "role": "user",
            "messageId": uuid.uuid4().hex,
            "parts": [{"kind": "text", "text": prompt}],
        }
        request = {
            "method": "SendMessage",
            "params": {
                "message": message,
                "blocking": blocking,
            },
        }
        if context_id:
            request["params"]["contextId"] = context_id

        await _write_json(self._writer, request)

        line = await self._reader.readline()
        if not line:
            raise ConnectionError("Server closed the connection")

        response = json.loads(line.decode("utf-8"))

        if "error" in response:
            err = response["error"]
            raise RuntimeError(
                f"A2A error ({err.get('code', '?')}): {err.get('message', 'unknown')}"
            )

        result = response.get("result", {})
        return _extract_text_from_result(result)

    async def raw_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send a raw JSON-RPC-style request and return the response."""
        if self._reader is None or self._writer is None:
            raise RuntimeError("Not connected — call connect() first")

        request = {"method": method, "params": params or {}}
        await _write_json(self._writer, request)

        line = await self._reader.readline()
        if not line:
            raise ConnectionError("Server closed the connection")

        return json.loads(line.decode("utf-8"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _write_json(writer: asyncio.StreamWriter, data: Any) -> None:
    """Write a JSON line to the stream."""
    writer.write(json.dumps(data, default=str).encode("utf-8") + b"\n")
    await writer.drain()


def _extract_text_from_result(result: dict[str, Any]) -> str:
    """Pull text content from an A2A task result dict."""
    parts: list[str] = []

    for artifact in result.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("kind") == "text":
                parts.append(part.get("text", ""))

    if parts:
        return "\n".join(parts)

    # Fallback: check status message.
    status = result.get("status", {})
    msg = status.get("message")
    if msg:
        for part in msg.get("parts", []):
            if part.get("kind") == "text":
                parts.append(part.get("text", ""))

    return "\n".join(parts) if parts else json.dumps(result)
