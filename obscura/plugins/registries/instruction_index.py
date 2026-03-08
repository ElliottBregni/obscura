"""Instruction index — tracks contributed instruction overlays."""

from __future__ import annotations

import logging
from typing import Any

from obscura.plugins.models import InstructionSpec

logger = logging.getLogger(__name__)


class InstructionIndex:
    """In-memory index of instruction overlays contributed by plugins."""

    def __init__(self) -> None:
        self._instructions: dict[str, InstructionSpec] = {}
        self._owner: dict[str, str] = {}

    def register(self, spec: InstructionSpec, plugin_id: str) -> None:
        self._instructions[spec.id] = spec
        self._owner[spec.id] = plugin_id

    def get(self, instruction_id: str) -> InstructionSpec | None:
        return self._instructions.get(instruction_id)

    def list_all(self) -> list[InstructionSpec]:
        return list(self._instructions.values())

    def for_scope(self, scope: str) -> list[InstructionSpec]:
        """Return instructions matching a scope, sorted by priority."""
        return sorted(
            [i for i in self._instructions.values() if i.scope == scope],
            key=lambda i: i.priority,
        )

    def assemble(self, scope: str = "global") -> str:
        """Assemble instruction text for a scope, ordered by priority."""
        instructions = self.for_scope(scope)
        return "\n\n".join(i.content for i in instructions)

    def filter_by_plugin(self, plugin_id: str) -> list[InstructionSpec]:
        return [
            i for iid, i in self._instructions.items()
            if self._owner.get(iid) == plugin_id
        ]

    def __len__(self) -> int:
        return len(self._instructions)
