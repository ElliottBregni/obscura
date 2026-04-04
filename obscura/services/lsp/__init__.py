"""obscura.services.lsp — Language Server Protocol client integration.

Provides code navigation (go-to-definition, find-references, hover,
document symbols) by spawning and communicating with language servers
via JSON-RPC over stdio.
"""

from obscura.services.lsp.client import LSPClient
from obscura.services.lsp.manager import LSPServerManager

__all__ = ["LSPClient", "LSPServerManager"]
