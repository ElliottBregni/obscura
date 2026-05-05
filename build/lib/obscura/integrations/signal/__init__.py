"""Signal integration via signal-cli REST bridge."""

from obscura.integrations.signal.adapter import SignalAdapter
from obscura.integrations.signal.client import SignalClient, SignalMessage
from obscura.integrations.signal.state import SignalState

__all__ = ["SignalAdapter", "SignalClient", "SignalMessage", "SignalState"]
