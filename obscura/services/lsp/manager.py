"""obscura.services.lsp.manager — Language server lifecycle management.

Manages spawning, health-checking, and shutting down language servers
per language. Servers are started on first use and cached.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from obscura.services.lsp.client import LSPClient

logger = logging.getLogger(__name__)

# Known language server commands by file extension.
_SERVER_COMMANDS: dict[str, list[str]] = {
    ".py": ["pyright-langserver", "--stdio"],
    ".ts": ["typescript-language-server", "--stdio"],
    ".tsx": ["typescript-language-server", "--stdio"],
    ".js": ["typescript-language-server", "--stdio"],
    ".jsx": ["typescript-language-server", "--stdio"],
    ".go": ["gopls", "serve"],
    ".rs": ["rust-analyzer"],
    ".java": ["jdtls"],
    ".rb": ["solargraph", "stdio"],
    ".c": ["clangd"],
    ".cpp": ["clangd"],
    ".h": ["clangd"],
}


def _detect_language(file_path: str) -> str:
    """Detect language from file extension."""
    return Path(file_path).suffix.lower()


class LSPServerManager:
    """Manages language server lifecycle per language.

    Servers are spawned lazily on first use and cached until shutdown.

    Usage::

        mgr = LSPServerManager(root_path="/project")
        client = await mgr.get_client("src/main.py")
        result = await client.goto_definition("src/main.py", 10, 5)
        await mgr.shutdown_all()
    """

    def __init__(self, root_path: str = "") -> None:
        self._root = root_path or str(Path.cwd())
        self._clients: dict[str, LSPClient] = {}  # ext → client

    async def get_client(self, file_path: str) -> LSPClient | None:
        """Get or create an LSP client for the given file's language."""
        ext = _detect_language(file_path)
        if ext in self._clients:
            return self._clients[ext]

        cmd = _SERVER_COMMANDS.get(ext)
        if cmd is None:
            return None

        binary = cmd[0]
        if shutil.which(binary) is None:
            logger.debug("LSP server %s not found in PATH", binary)
            return None

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            client = LSPClient(proc)
            await client.start(f"file://{self._root}")
            self._clients[ext] = client
            logger.info("LSP server started: %s for %s", binary, ext)
            return client
        except Exception:
            logger.warning("Failed to start LSP server: %s", binary, exc_info=True)
            return None

    async def shutdown_all(self) -> None:
        """Shutdown all running language servers."""
        for ext, client in self._clients.items():
            try:
                await client.shutdown()
            except Exception:
                logger.debug("LSP shutdown error for %s", ext, exc_info=True)
        self._clients.clear()

    @property
    def active_servers(self) -> list[str]:
        """Return list of active server extensions."""
        return list(self._clients.keys())
