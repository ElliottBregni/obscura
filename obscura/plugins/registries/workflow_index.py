"""Workflow index — tracks contributed workflows."""

from __future__ import annotations

import logging
from typing import Any

from obscura.plugins.models import WorkflowSpec

logger = logging.getLogger(__name__)


class WorkflowIndex:
    """In-memory index of workflows contributed by plugins."""

    def __init__(self) -> None:
        self._workflows: dict[str, WorkflowSpec] = {}
        self._owner: dict[str, str] = {}

    def register(self, spec: WorkflowSpec, plugin_id: str) -> None:
        self._workflows[spec.id] = spec
        self._owner[spec.id] = plugin_id

    def get(self, workflow_id: str) -> WorkflowSpec | None:
        return self._workflows.get(workflow_id)

    def get_owner(self, workflow_id: str) -> str | None:
        return self._owner.get(workflow_id)

    def list_all(self) -> list[WorkflowSpec]:
        return list(self._workflows.values())

    def filter_by_plugin(self, plugin_id: str) -> list[WorkflowSpec]:
        return [
            w for wid, w in self._workflows.items()
            if self._owner.get(wid) == plugin_id
        ]

    def executable_with(self, granted_capabilities: set[str]) -> list[WorkflowSpec]:
        """Return workflows whose required capabilities are all granted."""
        return [
            w for w in self._workflows.values()
            if set(w.required_capabilities).issubset(granted_capabilities)
        ]

    def __len__(self) -> int:
        return len(self._workflows)

    def __contains__(self, workflow_id: str) -> bool:
        return workflow_id in self._workflows
