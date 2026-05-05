"""obscura.composition.blocks — composable building blocks.

Each block has the signature ``async def install_*(session, config) -> None``
and is responsible for its own opt-out via config inspection.

A block's docstring must declare:
- **Reads:** which config / environment fields it consults
- **Writes:** which `AgentSession` fields it mutates
- **Resources:** what it registers via `session.register_resource(...)`
- **Opt-out:** the precise condition under which it returns without effect

`tests/composition/test_no_drift.py` enforces that legacy callsites in
surface modules have been removed once a block is extracted.
"""

from obscura.composition.blocks.browser_bridge import install_browser_bridge
from obscura.composition.blocks.plugins import install_plugin_tools
from obscura.composition.blocks.project_hooks import install_project_hooks
from obscura.composition.blocks.system_tools import install_system_tools
from obscura.composition.blocks.vector_memory import install_vector_memory

__all__ = [
    "install_browser_bridge",
    "install_plugin_tools",
    "install_project_hooks",
    "install_system_tools",
    "install_vector_memory",
]
