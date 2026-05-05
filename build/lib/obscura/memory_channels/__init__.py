"""Dynamic memory channel system.

Memory channels define targeted semantic memory queries that auto-activate
based on context signals (file paths, tool calls, keywords).
"""

from obscura.memory_channels.classifier import TurnClassifier
from obscura.memory_channels.config import (
    load_channels_from_config,
    load_channels_from_spec,
    merge_channels,
)
from obscura.memory_channels.models import (
    ChannelTriggers,
    ContextSignals,
    MemoryChannel,
)
from obscura.memory_channels.router import ContextRouter

__all__ = [
    "ChannelTriggers",
    "ContextRouter",
    "ContextSignals",
    "MemoryChannel",
    "TurnClassifier",
    "load_channels_from_config",
    "load_channels_from_spec",
    "merge_channels",
]
