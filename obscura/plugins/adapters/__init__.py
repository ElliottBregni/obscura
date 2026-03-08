"""Plugin adapter interface and implementations.

Adapters normalize different plugin packaging types (native Python, CLI binary,
SDK library, MCP server, HTTP service, content-only) into the same internal
resource models.

Each adapter implements ``PluginAdapter`` and produces normalized resources
from a ``PluginSpec``.
"""

from obscura.plugins.adapters.base import PluginAdapter
from obscura.plugins.adapters.native import NativeAdapter
from obscura.plugins.adapters.cli import CLIAdapter
from obscura.plugins.adapters.content import ContentAdapter

__all__ = [
    "PluginAdapter",
    "NativeAdapter",
    "CLIAdapter",
    "ContentAdapter",
]
