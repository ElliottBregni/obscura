"""Separated runtime registries for the Obscura plugin platform.

Each registry is a lightweight in-memory index for one resource type.
They share a common base but are queried independently.
"""

from obscura.plugins.registries.plugin_index import PluginIndex
from obscura.plugins.registries.capability_index import CapabilityIndex
from obscura.plugins.registries.tool_index import ToolIndex
from obscura.plugins.registries.workflow_index import WorkflowIndex
from obscura.plugins.registries.instruction_index import InstructionIndex

__all__ = [
    "PluginIndex",
    "CapabilityIndex",
    "ToolIndex",
    "WorkflowIndex",
    "InstructionIndex",
]
