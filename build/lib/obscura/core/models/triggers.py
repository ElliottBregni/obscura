"""Discriminated-union trigger payloads for ``DaemonAgent``.

Each variant carries a ``kind`` literal that matches one member of
``TriggerKind`` byte-for-byte, so legacy producers writing
``trigger.kind == "imessage"`` keep working.  Consumers should prefer
``match`` / ``isinstance`` dispatch over the enum check — see the
``Trigger`` alias below.

The legacy ``@dataclass`` triggers in ``obscura/agent/daemon_agent.py``
are unaffected by this module — they handle the in-process queue today.
This file pins the canonical wire-format model for the JSON envelope
that crosses process boundaries (Kairos events, the supervisor RPC, the
push-notification sink).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import Field

from obscura.core.enums.messaging import TriggerKind
from obscura.core.models._base import ObscuraModel


class IMessageTrigger(ObscuraModel):
    """An iMessage arrived for the configured contact set."""

    kind: Literal[TriggerKind.IMESSAGE] = TriggerKind.IMESSAGE
    chat_id: str
    text: str
    sender: str


class MessageTrigger(ObscuraModel):
    """A generic platform-message trigger (Slack, WhatsApp, ...)."""

    kind: Literal[TriggerKind.MESSAGE] = TriggerKind.MESSAGE
    body: str
    metadata: Mapping[str, str] = Field(default_factory=dict)


class EmailTrigger(ObscuraModel):
    """An inbound email arrived for the agent."""

    kind: Literal[TriggerKind.EMAIL] = TriggerKind.EMAIL
    subject: str
    from_address: str
    body: str


class StopTrigger(ObscuraModel):
    """Sentinel that drains the trigger queue and exits the daemon."""

    kind: Literal[TriggerKind.STOP] = TriggerKind.STOP


Trigger = Annotated[
    IMessageTrigger | MessageTrigger | EmailTrigger | StopTrigger,
    Field(discriminator="kind"),
]


__all__ = [
    "EmailTrigger",
    "IMessageTrigger",
    "MessageTrigger",
    "StopTrigger",
    "Trigger",
]
