"""Platform-agnostic messaging abstractions."""

from obscura.integrations.messaging.adapter import MessagePlatformAdapter
from obscura.integrations.messaging.factory import get_adapter, register_adapter
from obscura.integrations.messaging.identity import build_conversation_key, normalize_identity
from obscura.integrations.messaging.models import ConversationState, PlatformMessage
from obscura.integrations.messaging.store import (
    ConversationStore,
    DaemonLockStore,
    MessageDedupeStore,
)

__all__ = [
    "MessagePlatformAdapter",
    "ConversationState",
    "PlatformMessage",
    "ConversationStore",
    "DaemonLockStore",
    "MessageDedupeStore",
    "normalize_identity",
    "build_conversation_key",
    "get_adapter",
    "register_adapter",
]
