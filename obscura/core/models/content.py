"""Discriminated Pydantic union for message content blocks.

Replaces the legacy ``ContentBlock`` dataclass with a tagged union of four
frozen Pydantic variants discriminated on ``kind``. Internal consumers
should ``match`` (or ``isinstance``) on the variant rather than read
``block.kind`` strings; the ``ContentBlock`` type alias is the union all
four variants flow through.

Wire format is byte-identical: ``model_dump()`` on any variant matches the
old ``dataclasses.asdict()`` output for the same logical block, and the
discriminator value comes from ``ContentBlockKind`` whose values are the
same lowercase strings used today (``"text"``, ``"thinking"``,
``"tool_use"``, ``"tool_result"``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, Union

from pydantic import Field

from obscura.core.enums.tools import ContentBlockKind
from obscura.core.models._base import ObscuraModel


class TextBlock(ObscuraModel):
    """Plain assistant or user text."""

    kind: Literal[ContentBlockKind.TEXT] = ContentBlockKind.TEXT
    text: str = ""


class ThinkingBlock(ObscuraModel):
    """Reasoning/thinking content separate from user-visible text."""

    kind: Literal[ContentBlockKind.THINKING] = ContentBlockKind.THINKING
    text: str = ""


class ToolUseBlock(ObscuraModel):
    """A tool invocation emitted by the assistant."""

    kind: Literal[ContentBlockKind.TOOL_USE] = ContentBlockKind.TOOL_USE
    tool_use_id: str = ""
    tool_name: str = ""
    args: Mapping[str, Any] = Field(default_factory=dict)

    @property
    def tool_input(self) -> Mapping[str, Any]:
        """Backwards-compatible alias for ``args`` used by legacy callers."""
        return self.args


class ToolResultBlock(ObscuraModel):
    """Output of a tool invocation, fed back to the assistant."""

    kind: Literal[ContentBlockKind.TOOL_RESULT] = ContentBlockKind.TOOL_RESULT
    tool_use_id: str = ""
    content: str | list[TextBlock] = ""
    is_error: bool = False

    @property
    def text(self) -> str:
        """Backwards-compatible alias for legacy ``ContentBlock.text`` access.

        Returns ``content`` directly when it is a string, otherwise the
        concatenated text of the inline ``TextBlock`` parts.
        """
        body = self.content
        if isinstance(body, str):
            return body
        return "".join(part.text for part in body)


ContentBlock = Annotated[
    Union[TextBlock, ThinkingBlock, ToolUseBlock, ToolResultBlock],
    Field(discriminator="kind"),
]
"""Discriminated union over the four block variants."""


__all__ = [
    "ContentBlock",
    "ContentBlockKind",
    "TextBlock",
    "ThinkingBlock",
    "ToolResultBlock",
    "ToolUseBlock",
]
