"""obscura.composition.blocks — composable building blocks.

Each block has the signature ``async def install_*(session, config) -> None``
and is responsible for its own opt-out via config inspection.

A block's docstring must declare:
- **Reads:** which config / environment fields it consults
- **Writes:** which `AgentSession` fields it mutates
- **Resources:** what it registers via `session.register_resource(...)`
- **Opt-out:** the precise condition under which it returns without effect

`tests/composition/test_block_contracts.py` enforces presence of these
docstring sections. `test_no_drift.py` enforces that legacy callsites in
surface modules have been removed once a block is extracted.
"""

from obscura.composition.blocks.plugins import install_plugin_tools

__all__ = ["install_plugin_tools"]
