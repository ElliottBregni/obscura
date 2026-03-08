"""Content adapter — for plugins that contribute only instructions/workflows.

These plugins have no executable tool handlers. They contribute instruction
overlays, workflow definitions, and/or policy hints.
"""

from __future__ import annotations

import logging
from typing import Any

from obscura.plugins.models import PluginSpec

logger = logging.getLogger(__name__)


class ContentAdapter:
    """Adapter for content-only plugins (runtime_type == 'content').

    No handlers to resolve — the manifest's instructions, workflows, and
    policy hints are already captured in the PluginSpec.
    """

    def can_handle(self, spec: PluginSpec) -> bool:
        return spec.runtime_type == "content"

    async def load(self, spec: PluginSpec, config: dict[str, Any]) -> dict[str, Any]:
        logger.debug("Content plugin %s loaded (%d instructions, %d workflows)",
                      spec.id, len(spec.instructions), len(spec.workflows))
        return {"handlers": {}}

    async def healthcheck(self, spec: PluginSpec) -> bool:
        return True  # content plugins are always healthy

    async def teardown(self, spec: PluginSpec) -> None:
        pass
