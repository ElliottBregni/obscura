"""LSP server manager — starts and manages language server processes.

Provides ``LSPServerManager`` which boots one LSP server process per workspace
root and routes JSON-RPC requests through it.  Currently supports pyright
(Python) with automatic fallback to pylsp.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CONTENT_LENGTH_RE = re.compile(rb"Content-Length:\s*(\d+)", re.IGNORECASE)


def _find_workspace_root(file_path: str) -> str:
    """Walk up from *file_path* to find a workspace root.

    Markers: pyproject.toml, setup.py, setup.cfg, Cargo.toml, package.json, .git.
    Falls back to the file's parent directory if none is found.
    """
    markers = {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "Cargo.toml",
        "package.json",
        ".git",
    }
    p = Path(file_path).resolve().parent
    for parent in [p, *p.parents]:
        if any((parent / m).exists() for m in markers):
            return str(parent)
    return str(p)


class LSPClient:
    """Async JSON-RPC client talking to a language server over stdio."""

    def __init__(
        self, process: asyncio.subprocess.Process, workspace_root: str
    ) -> None:
        self._process = process
        self._workspace_root = workspace_root
        self._req_id = 0
        self._initialized = False
        self._opened_uris: set[str] = set()

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    @property
    def is_alive(self) -> bool:
        return self._process.returncode is None

    async def _write(self, msg: dict[str, Any]) -> None:
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode()
        assert self._process.stdin is not None  # noqa: S101
        self._process.stdin.write(header + body)
        await self._process.stdin.drain()

    async def _read_one(self, timeout: float = 15.0) -> dict[str, Any] | None:
        """Read one JSON-RPC message from stdout."""
        assert self._process.stdout is not None  # noqa: S101
        header = b""
        while True:
            try:
                ch = await asyncio.wait_for(
                    self._process.stdout.read(1), timeout=timeout
                )
            except TimeoutError:
                logger.debug(
                    "suppressed exception in _read_one (header read)", exc_info=True
                )
                return None
            if not ch:
                return None
            header += ch
            if header.endswith(b"\r\n\r\n"):
                break
        m = _CONTENT_LENGTH_RE.search(header)
        if not m:
            return None
        length = int(m.group(1))
        body = b""
        while len(body) < length:
            try:
                chunk = await asyncio.wait_for(
                    self._process.stdout.read(length - len(body)), timeout=timeout
                )
            except TimeoutError:
                logger.debug(
                    "suppressed exception in _read_one (body read)", exc_info=True
                )
                break
            if not chunk:
                break
            body += chunk
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            logger.debug("lsp: malformed JSON body: %r", body[:200])
            return None

    async def _request(self, method: str, params: Any, timeout: float = 15.0) -> Any:
        """Send a JSON-RPC request and wait for its response."""
        req_id = self._next_id()
        await self._write(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        )
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"LSP request '{method}' timed out")
            msg = await self._read_one(timeout=min(remaining, 5.0))
            if msg is None:
                raise TimeoutError(f"LSP request '{method}': no response")
            if "id" not in msg or msg["id"] != req_id:
                continue  # skip notifications and other responses
            if "error" in msg:
                raise RuntimeError(f"LSP error in '{method}': {msg['error']}")
            return msg.get("result")

    async def _notify(self, method: str, params: Any) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def initialize(self) -> None:
        """Send initialize + initialized (idempotent)."""
        if self._initialized:
            return
        await self._request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": f"file://{self._workspace_root}",
                "capabilities": {
                    "textDocument": {
                        "definition": {"dynamicRegistration": False},
                        "references": {"dynamicRegistration": False},
                        "hover": {
                            "dynamicRegistration": False,
                            "contentFormat": ["markdown", "plaintext"],
                        },
                        "documentSymbol": {"dynamicRegistration": False},
                    }
                },
                "initializationOptions": {},
            },
            timeout=30.0,
        )
        await self._notify("initialized", {})
        self._initialized = True

    async def _open_file(self, file_path: str) -> None:
        """Send textDocument/didOpen (idempotent per URI)."""
        uri = f"file://{Path(file_path).resolve()}"
        if uri in self._opened_uris:
            return
        try:
            text = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.debug("lsp: cannot read %s: %s", file_path, exc)
            return
        ext = Path(file_path).suffix.lower()
        lang_id = {
            ".py": "python",
            ".ts": "typescript",
            ".tsx": "typescriptreact",
            ".js": "javascript",
            ".jsx": "javascriptreact",
            ".go": "go",
            ".rs": "rust",
            ".c": "c",
            ".cpp": "cpp",
            ".java": "java",
        }.get(ext, "plaintext")
        await self._notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": lang_id,
                    "version": 1,
                    "text": text,
                }
            },
        )
        self._opened_uris.add(uri)

    @staticmethod
    def _lsp_pos(line: int, character: int) -> dict[str, int]:
        """Convert 1-based tool coordinates to 0-based LSP positions."""
        return {"line": max(0, line - 1), "character": max(0, character - 1)}

    async def goto_definition(self, file_path: str, line: int, character: int) -> Any:
        await self.initialize()
        await self._open_file(file_path)
        return await self._request(
            "textDocument/definition",
            {
                "textDocument": {"uri": f"file://{Path(file_path).resolve()}"},
                "position": self._lsp_pos(line, character),
            },
        )

    async def find_references(self, file_path: str, line: int, character: int) -> Any:
        await self.initialize()
        await self._open_file(file_path)
        return await self._request(
            "textDocument/references",
            {
                "textDocument": {"uri": f"file://{Path(file_path).resolve()}"},
                "position": self._lsp_pos(line, character),
                "context": {"includeDeclaration": True},
            },
        )

    async def hover(self, file_path: str, line: int, character: int) -> Any:
        await self.initialize()
        await self._open_file(file_path)
        return await self._request(
            "textDocument/hover",
            {
                "textDocument": {"uri": f"file://{Path(file_path).resolve()}"},
                "position": self._lsp_pos(line, character),
            },
        )

    async def document_symbols(self, file_path: str) -> Any:
        await self.initialize()
        await self._open_file(file_path)
        return await self._request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": f"file://{Path(file_path).resolve()}"}},
            timeout=30.0,
        )

    async def shutdown(self) -> None:
        try:
            await self._request("shutdown", None, timeout=5.0)
            await self._notify("exit", None)
        except Exception:
            logger.debug(
                "suppressed exception in shutdown (graceful stop)", exc_info=True
            )
        try:
            if self._process.returncode is None:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
        except Exception:
            logger.debug(
                "suppressed exception in shutdown (process terminate)", exc_info=True
            )


class LSPServerManager:
    """Manages one LSPClient per workspace root."""

    def __init__(self) -> None:
        self._root = str(Path.cwd())
        self._clients: dict[str, LSPClient] = {}

    async def get_client(self, file_path: str) -> LSPClient | None:
        """Return a live LSPClient for the workspace containing file_path.

        Starts a new server if none is running. Returns None if no server binary found.
        """
        workspace_root = _find_workspace_root(file_path)
        existing = self._clients.get(workspace_root)
        if existing is not None:
            if existing.is_alive:
                return existing
            del self._clients[workspace_root]

        server_cmd = shutil.which("pyright") or shutil.which("pylsp")
        if not server_cmd:
            logger.warning("lsp: no LSP server found on PATH (tried pyright, pylsp)")
            return None

        try:
            process = await asyncio.create_subprocess_exec(
                server_cmd,
                "--stdio",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=workspace_root,
            )
        except OSError as exc:
            logger.warning("lsp: failed to start %s: %s", server_cmd, exc)
            return None

        client = LSPClient(process, workspace_root)
        self._clients[workspace_root] = client
        logger.debug(
            "lsp: started %s for %s (pid=%s)", server_cmd, workspace_root, process.pid
        )
        return client
