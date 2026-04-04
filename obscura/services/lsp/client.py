"""obscura.services.lsp.client — JSON-RPC LSP client over stdio.

Communicates with language servers using the Language Server Protocol
(LSP) specification via stdin/stdout JSON-RPC 2.0.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class LSPClient:
    """JSON-RPC 2.0 client for communicating with a language server."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._initialized = False

    async def start(self, root_uri: str) -> None:
        """Send initialize + initialized handshake."""
        self._reader_task = asyncio.create_task(self._read_loop())
        result = await self.request(
            "initialize",
            {
                "processId": None,
                "rootUri": root_uri,
                "capabilities": {
                    "textDocument": {
                        "definition": {"dynamicRegistration": False},
                        "references": {"dynamicRegistration": False},
                        "hover": {"contentFormat": ["plaintext"]},
                        "documentSymbol": {"dynamicRegistration": False},
                    },
                },
            },
        )
        await self.notify("initialized", {})
        self._initialized = True
        return result

    async def shutdown(self) -> None:
        """Send shutdown + exit."""
        if self._initialized:
            try:
                await self.request("shutdown", None)
                await self.notify("exit", None)
            except Exception:
                pass
        if self._reader_task:
            self._reader_task.cancel()
        if self._process.returncode is None:
            self._process.terminate()

    async def request(self, method: str, params: Any) -> Any:
        """Send a JSON-RPC request and await the response."""
        self._request_id += 1
        rid = self._request_id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[rid] = future
        self._send(msg)
        return await asyncio.wait_for(future, timeout=30.0)

    async def notify(self, method: str, params: Any) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        self._send(msg)

    def _send(self, msg: dict[str, Any]) -> None:
        """Write a JSON-RPC message to the server's stdin."""
        assert self._process.stdin is not None
        body = json.dumps(msg)
        header = f"Content-Length: {len(body)}\r\n\r\n"
        self._process.stdin.write((header + body).encode("utf-8"))

    async def _read_loop(self) -> None:
        """Read JSON-RPC messages from the server's stdout."""
        assert self._process.stdout is not None
        try:
            while True:
                header = await self._process.stdout.readline()
                if not header:
                    break
                if header.startswith(b"Content-Length:"):
                    length = int(header.split(b":")[1].strip())
                    await self._process.stdout.readline()  # empty line
                    body = await self._process.stdout.readexactly(length)
                    msg = json.loads(body)
                    rid = msg.get("id")
                    if rid is not None and rid in self._pending:
                        if "error" in msg:
                            self._pending[rid].set_exception(
                                RuntimeError(msg["error"].get("message", "LSP error")),
                            )
                        else:
                            self._pending[rid].set_result(msg.get("result"))
                        del self._pending[rid]
        except (asyncio.CancelledError, ConnectionError):
            pass

    # --- High-level operations ---

    async def goto_definition(self, file_path: str, line: int, character: int) -> Any:
        """Find definition of symbol at position."""
        return await self.request(
            "textDocument/definition",
            {
                "textDocument": {"uri": f"file://{file_path}"},
                "position": {"line": line - 1, "character": character - 1},
            },
        )

    async def find_references(self, file_path: str, line: int, character: int) -> Any:
        """Find all references to symbol at position."""
        return await self.request(
            "textDocument/references",
            {
                "textDocument": {"uri": f"file://{file_path}"},
                "position": {"line": line - 1, "character": character - 1},
                "context": {"includeDeclaration": True},
            },
        )

    async def hover(self, file_path: str, line: int, character: int) -> Any:
        """Get hover information for symbol at position."""
        return await self.request(
            "textDocument/hover",
            {
                "textDocument": {"uri": f"file://{file_path}"},
                "position": {"line": line - 1, "character": character - 1},
            },
        )

    async def document_symbols(self, file_path: str) -> Any:
        """Get all symbols in a document."""
        return await self.request(
            "textDocument/documentSymbol",
            {
                "textDocument": {"uri": f"file://{file_path}"},
            },
        )
